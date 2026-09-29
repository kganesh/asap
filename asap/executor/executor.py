"""The executor: the only component holding write credentials.

It behaves like a reconciler, not a script:
* dry-run computes the diff and the precondition (resourceVersion) without changing anything
* execute re-checks the precondition, then applies; every step is recorded against the
  proposal_id so a retry or redelivery can never apply the same action twice
* rollbacks go through GitOps (a revert that Argo CD syncs), scaling through HPA minReplicas,
  so cluster controllers don't silently undo the fix
* verification waits, then re-reads the SLI; revert rules are per action
"""

from __future__ import annotations

import httpx

from ..control.store import StateStore
from ..models import DryRun, Proposal
from ..simclient import SimClient

VERIFY_WAIT_MINUTES = 5


class PreconditionFailed(Exception):
    pass


class Executor:
    def __init__(self, sim_url: str, store: StateStore) -> None:
        self.sim = SimClient.executor(sim_url)  # write credential lives only here
        self.store = store
        self.crash_after_apply = False  # test hook: simulate a crash between apply and bookkeeping

    # ------------------------------------------------------------------ dry run
    def dry_run(self, p: Proposal) -> DryRun:
        try:
            st = self.sim.state(p.target)
        except httpx.HTTPStatusError:
            return DryRun(False, {}, None, errors=[f"target {p.target} not found"])
        rv = st["resourceVersion"]
        errors: list[str] = []
        diff: dict = {}
        crosses = False
        if p.action == "rollback":
            hist = self.sim.history(p.target)
            target = next((r for r in hist if r["revision"] == p.params["to_revision"]), None)
            current = next(r for r in hist if r["current"])
            if st["kind"] != "Deployment":
                errors.append("rollback is only supported for Deployments")
            if not st["gitops_managed"]:
                errors.append("target is not GitOps-managed; refusing an out-of-band rollback")
            if target is None:
                errors.append(f"revision {p.params['to_revision']} does not exist")
            elif target["revision"] == current["revision"] or target["version"] == current["version"]:
                errors.append("target revision is already running")
            else:
                newer = [r for r in hist if target["revision"] < r["revision"] <= current["revision"]]
                crosses = any(r["annotations"].get("asap.io/schema-migration") == "true" for r in newer)
                diff = {"image": f"{current['image']} -> {target['image']}",
                        "via": "git revert + Argo CD sync", "reverts_revisions": [r["revision"] for r in newer]}
        elif p.action == "scale":
            hpa = st.get("hpa")
            want = p.params["replicas"]
            if not hpa:
                errors.append("target has no HPA")
            elif want > hpa["maxReplicas"]:
                errors.append(f"replicas {want} exceeds HPA maxReplicas {hpa['maxReplicas']}")
            else:
                diff = {"hpa.minReplicas": f"{hpa['minReplicas']} -> {want}",
                        "replicas": f"{st['replicas']} -> {max(want, st['replicas'])}"}
        elif p.action == "restart":
            if st["kind"] != "Deployment":
                errors.append("restart is only supported for Deployments")
            diff = {"pods_cycled": st["replicas"], "strategy": "RollingUpdate maxUnavailable=1"}
        elif p.action == "cache_flush":
            diff = {"cache": p.target, "key_prefix": p.params["key_prefix"]}
        else:
            errors.append(f"unsupported action {p.action}")
        return DryRun(not errors, diff, rv, crosses, errors)

    # ------------------------------------------------------------------ execute (idempotent)
    def execute(self, p: Proposal, precondition_rv: str | None) -> dict:
        done = self.store.step_status(p.proposal_id, "apply")
        if done and done["status"] == "done":
            return {"applied": True, "idempotent_replay": True, **done["detail"]}
        # precondition: nobody changed the target since the dry-run / approval
        st = self.sim.state(p.target)
        if precondition_rv is not None and st["resourceVersion"] != precondition_rv:
            raise PreconditionFailed(f"{p.target} changed since approval (resourceVersion {precondition_rv} -> "
                                     f"{st['resourceVersion']}); re-evaluation required")
        self.store.mark_step(p.proposal_id, "apply", "started", {})
        prev = {"hpa_min": (st.get("hpa") or {}).get("minReplicas"), "revision": st["current_revision"]}
        if p.action == "rollback":
            res = self.sim.post("/admin/gitops/revert", {"deployment": p.target, "to_revision": p.params["to_revision"]})
        elif p.action == "scale":
            res = self.sim.post(f"/admin/hpa/{p.target}", {"min_replicas": p.params["replicas"]})
        elif p.action == "restart":
            res = self.sim.post(f"/admin/rollout-restart/{p.target}")
        else:
            res = self.sim.post("/admin/cache/flush", {"cache": p.target, "key_prefix": p.params["key_prefix"]})
        if self.crash_after_apply:
            self.crash_after_apply = False
            raise RuntimeError("executor crashed after apply (simulated)")
        detail = {"result": res, "previous": prev, "applied_at": self.sim.now()}
        self.store.mark_step(p.proposal_id, "apply", "done", detail)
        return {"applied": True, "idempotent_replay": False, **detail}

    def recover(self, p: Proposal) -> dict | None:
        """After a crash: a step left 'started' is reconciled by reading actual state, never re-applied blindly."""
        s = self.store.step_status(p.proposal_id, "apply")
        if not s or s["status"] != "started":
            return None
        st = self.sim.state(p.target)
        applied = {
            "rollback": lambda: any(r["current"] and "(asap)" in r["change_cause"] for r in self.sim.history(p.target)),
            "scale": lambda: (st.get("hpa") or {}).get("minReplicas") == p.params.get("replicas"),
        }.get(p.action, lambda: False)()
        detail = {"reconciled": True, "observed_applied": applied}
        self.store.mark_step(p.proposal_id, "apply", "done" if applied else "failed", detail)
        return detail

    # ------------------------------------------------------------------ verify / revert
    def verify(self, root_service: str, alertnames: set[str]) -> dict:
        before = [a for a in self.sim.alerts() if a["labels"]["service"] == root_service]
        # Simulator stand-in for waiting VERIFY_WAIT_MINUTES of wall clock.
        self.sim.post("/admin/clock/advance", {"minutes": VERIFY_WAIT_MINUTES})
        after = [a for a in self.sim.alerts() if a["labels"]["service"] == root_service]
        still = sorted({a["labels"]["alertname"] for a in after})
        return {"waited_minutes": VERIFY_WAIT_MINUTES, "root_service": root_service,
                "firing_before": sorted({a["labels"]["alertname"] for a in before}), "firing_after": still,
                "recovered": not (set(still) & alertnames) and not still,
                "sli_after": {a["labels"]["alertname"]: a["annotations"]["description"] for a in after}}

    def revert(self, p: Proposal, execution: dict) -> dict:
        if p.action == "scale":
            prev = execution["previous"]["hpa_min"]
            res = self.sim.post(f"/admin/hpa/{p.target}", {"min_replicas": prev})
            return {"reverted": True, "rule": "restore previous HPA minReplicas", "result": res}
        rules = {
            "rollback": "never auto-revert a rollback (it would redeploy the bad version); escalate",
            "restart": "nothing to revert after a restart; escalate",
            "cache_flush": "a cache flush cannot be undone; escalate",
        }
        return {"reverted": False, "rule": rules[p.action]}
