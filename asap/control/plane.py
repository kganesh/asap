"""The safety control plane. Deterministic; the LLM never runs here.

submit(proposal) -> Decision:
  1. evidence check    - every cited evidence ID was issued in this run (anti-hallucination)
  2. scope check       - target is in the incident's dependency scope, derived here from the graph
  3. context fetch     - target state, capacity, budget, controls: fetched by the control plane,
                         never taken from the agent's proposal
  4. dry-run           - executor computes diff + precondition without changing anything
  5. blast radius      - deterministic score
  6. policy (Rego)     - allow | require_approval | deny ; evaluator failure => deny (fail closed)
"""

from __future__ import annotations

import os

import httpx

from ..executor.executor import Executor
from ..models import Decision, DryRun, Proposal, RunState
from ..simclient import SimClient
from .policy import PolicyEngine, PolicyUnavailable
from .store import StateStore, StoreUnavailable

TIER_WEIGHT = {0: 40, 1: 20, 2: 10}
REVERSIBILITY = {"rollback": 15, "cache_flush": 25, "restart": 10, "scale_up": 0, "scale_down": 15}


def blast_radius(action: str, target_state: dict, upstream: list[str], params: dict) -> int:
    kind = action
    if action == "scale":
        kind = "scale_up" if params.get("replicas", 0) > target_state.get("replicas", 0) else "scale_down"
    score = TIER_WEIGHT.get(target_state.get("tier", 2), 10)
    score += REVERSIBILITY.get(kind, 25)
    score += min(20, 5 * len(upstream))
    if action == "restart":
        score += 15  # every pod is cycled
    return min(100, score)


class Controls:
    """Global switches. In production these are flags in the control-plane config service."""

    def __init__(self) -> None:
        self.kill_switch = os.environ.get("ASAP_KILL_SWITCH", "0") == "1"
        self.change_freeze = os.environ.get("ASAP_CHANGE_FREEZE", "0") == "1"


class ControlPlane:
    def __init__(self, sim_url: str, store: StateStore, executor: Executor, policy: PolicyEngine,
                 controls: Controls | None = None) -> None:
        self.sim = SimClient.reader(sim_url)  # the control plane reads context with its own credential
        self.store = store
        self.executor = executor
        self.policy = policy
        self.controls = controls or Controls()

    # ------------------------------------------------------------------ scope (derived, not trusted)
    def incident_scope(self, run: RunState) -> set[str]:
        """Root service plus everything downstream of it. Upstream services are victims, not targets."""
        scope = {run.root_service}
        frontier = [run.root_service]
        while frontier:
            s = frontier.pop()
            try:
                downstream = self.sim.dependencies(s)["downstream"]
            except httpx.HTTPError:
                continue
            for d in downstream:
                if d not in scope:
                    scope.add(d)
                    frontier.append(d)
        return scope

    def build_input(self, run: RunState, p: Proposal, dry: DryRun) -> tuple[dict, int]:
        now = self.sim.now()
        try:
            st = self.sim.state(p.target)
            upstream = self.sim.dependencies(p.target)["upstream"]
        except httpx.HTTPError:
            st, upstream = {"kind": "Unknown", "tier": 0, "replicas": 0}, []
        cited = [run.evidence.get(e) for e in p.evidence_ids]
        valid = bool(p.evidence_ids) and all(cited)
        has_metric = valid and any(e.kind == "metric" for e in cited if e)
        budget = self.store.budget(p.target, now)
        budget["circuit_open"] = self.store.circuit_open(p.target, p.action, now)
        br = blast_radius(p.action, st, upstream, p.params)
        policy_input = {
            "action": {"type": p.action, "target": p.target, "params": p.params},
            "target": {"kind": st.get("kind"), "tier": st.get("tier"), "replicas": st.get("replicas"),
                       "hpa": st.get("hpa"), "pdb": st.get("pdb"), "owners": st.get("owners", []),
                       "gitops_managed": st.get("gitops_managed")},
            "cluster": {"pods_used": st.get("cluster_pods_used", 0),
                        "pods_allocatable": st.get("cluster_pods_allocatable", 1)},
            "dry_run": {"ok": dry.ok, "crosses_migration": dry.crosses_migration, "diff": dry.diff,
                        "errors": dry.errors},
            "diagnosis": {"confidence": p.diagnosis_confidence},
            "evidence": {"valid": valid, "has_metric": has_metric, "count": len(p.evidence_ids)},
            "scope": {"in_incident_scope": p.target in self.incident_scope(run)},
            "budget": budget,
            "controls": {"kill_switch": self.controls.kill_switch, "change_freeze": self.controls.change_freeze},
            "blast_radius": br,
        }
        return policy_input, br

    def submit(self, run: RunState, p: Proposal) -> Decision:
        dry = self.executor.dry_run(p)
        try:
            policy_input, br = self.build_input(run, p, dry)
        except StoreUnavailable as e:
            return Decision(p.proposal_id, "deny", 3, [f"{e}: failing closed"], 100, dry, {}, self.policy.name)
        try:
            out = self.policy.evaluate(policy_input)
        except PolicyUnavailable as e:
            return Decision(p.proposal_id, "deny", 3, [f"policy engine unavailable ({e}): failing closed"], br, dry,
                            policy_input, self.policy.name)
        tier = {"allow": 1, "require_approval": 2, "deny": 3}[out["decision"]]
        reasons = out["deny"] if tier == 3 else out["require_approval"] if tier == 2 else [
            "within tier-1 bounds: reversible, capacity-safe, metric evidence"]
        return Decision(p.proposal_id, out["decision"], tier, reasons, br, dry, policy_input, self.policy.name,
                        required_approvals=out["required_approvals"] if tier == 2 else 0)

    def revalidate(self, run: RunState, p: Proposal, original: Decision) -> tuple[bool, str, Decision]:
        """Right before execution: fresh dry-run + fresh policy with fresh context. Any drift or a worse verdict aborts."""
        fresh = self.submit(run, p)
        if fresh.tier > original.tier:
            return False, f"policy verdict worsened since proposal ({original.verdict} -> {fresh.verdict}): " \
                          f"{'; '.join(fresh.reasons)}", fresh
        if original.dry_run and fresh.dry_run and \
                original.dry_run.precondition_resource_version != fresh.dry_run.precondition_resource_version:
            return False, "target changed since the proposal was approved; approval voided", fresh
        return True, "preconditions and policy unchanged", fresh
