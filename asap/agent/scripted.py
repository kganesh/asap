"""DeterministicReasoner: a keyless stand-in for the LLM.

It is NOT scenario-aware. It follows a fixed SRE checklist, reads the actual tool results from run
state, and applies explicit rules (temporal deploy correlation, saturation, downstream dominance).
It goes through exactly the same tool gateway, control plane, approval and executor as a real LLM,
so every guardrail is exercised. Use it for deterministic demos and CI; set ANTHROPIC_API_KEY (or an
OpenAI-compatible endpoint) to run the same workflow with a real model.
"""

from __future__ import annotations

import math

from ..models import Evidence, RunState
from ..signals import DOMINANT_DOWNSTREAM_SHARE, SUSPECT_DEPLOY_WINDOW_MIN
from .llm import LLMResponse

# The checklist's query shapes.
METRIC_WINDOW_MIN = 30  # golden-signal look-back
RECENT_WINDOW_MIN = 15  # traces and logs look-back
LOG_CLUSTERS = 8

# Decision thresholds of this rule-based stand-in model. A real LLM makes these judgments itself; the
# deterministic protections (policy, scope, evidence relevance) do not depend on them.
ERROR_RATIO_INCIDENT = 0.05  # 5xx ratio worth calling a defect
LATENCY_INCIDENT_S = 1.0  # p99 worth calling a latency incident
THROTTLE_SATURATED = 0.25  # share of CFS periods throttled
SCALE_FACTOR = 2  # proposed replicas = double, within the HPA max
CONFIDENCE = {"bad_deploy": 0.86, "dependency_failure": 0.8, "saturation": 0.8, "unknown": 0.4}
NO_ACTION_REASON_CHARS = 400  # the no_action tool's schema limit


class DeterministicReasoner:
    name = "deterministic-reasoner"
    model = "rules-v1 (no LLM)"

    def __init__(self) -> None:
        self._n = 0

    # ------------------------------------------------------------------ helpers
    def _call(self, name: str, args: dict, thought: str) -> LLMResponse:
        """As with a live model, the reasoning travels in the tool's required `reasoning` field."""
        self._n += 1
        return LLMResponse(name, {"reasoning": thought or f"{name} per checklist", **args}, f"det_{self._n}", "",
                           model=self.model)

    @staticmethod
    def _find(run: RunState, tool: str, **match: object) -> Evidence | None:
        for e in run.evidence.values():
            if e.tool == tool and all(e.args.get(k) == v for k, v in match.items()):
                return e
        return None

    def next(self, system: str, messages: list[dict], tools: list[dict], run: RunState, phase: str,
             timeout_s: float) -> LLMResponse:
        s = run.root_service
        names = {a["labels"]["alertname"] for a in run.alerts if a["labels"]["service"] == s}
        if phase == "PLAN":
            if run.replans == 0 and run.plan is None:
                hyps = []
                if "HighErrorRate" in names:
                    hyps += ["bad deploy: a recent revision introduced a defect", "failing downstream dependency"]
                if names & {"HighLatencyP99", "CPUThrottlingHigh"}:
                    hyps += ["saturation: CPU throttling under load", "bad deploy: recent revision slowed requests"]
                return self._call("submit_plan", {"hypotheses": hyps or ["unknown"], "steps": [
                    f"deployment history of {s}", "error ratio, p99 latency, CPU throttling",
                    "traces: which downstream dominates", "error and warning log clusters"]},
                    f"Alerts on {s}: {sorted(names)}. Start with change history and golden signals.")
        if phase == "INVESTIGATE":
            return self._investigate(run, s)
        if phase == "PROPOSE":
            return self._propose(run)
        return self._call("no_action", {"reason": f"unexpected phase {phase}"}, "")

    # ------------------------------------------------------------------ investigate
    def _investigate(self, run: RunState, s: str) -> LLMResponse:
        checklist = [
            ("get_deployment_history", {"service": s}, "What changed recently?"),
            ("query_metrics", {"metric": "error_ratio", "service": s, "window_minutes": METRIC_WINDOW_MIN},
             "Error ratio and when it moved."),
            ("query_metrics", {"metric": "latency_p99_seconds", "service": s, "window_minutes": METRIC_WINDOW_MIN},
             "Latency shape."),
            ("query_metrics", {"metric": "cpu_throttle_ratio", "service": s, "window_minutes": METRIC_WINDOW_MIN},
             "Saturation check."),
            ("get_traces", {"service": s, "window_minutes": RECENT_WINDOW_MIN}, "Where does request time go?"),
            ("search_logs", {"service": s, "level": "ERROR", "window_minutes": RECENT_WINDOW_MIN, "limit": LOG_CLUSTERS},
             "New exceptions?"),
            ("search_logs", {"service": s, "level": "WARN", "window_minutes": RECENT_WINDOW_MIN, "limit": LOG_CLUSTERS},
             "Warnings: queueing, slow calls?"),
            ("get_resource_state", {"service": s}, "Replicas, HPA bounds, PDB."),
        ]
        for tool, args, why in checklist:
            key = {k: v for k, v in args.items() if k in ("service", "metric", "level")}
            if not self._find(run, tool, **key):
                return self._call(tool, args, why)
        traces = self._find(run, "get_traces", service=s)
        top = (traces.result.get("downstream") or [None])[0] if traces else None
        if top and top["share_of_root_p99"] >= DOMINANT_DOWNSTREAM_SHARE:
            d = top["peer.service"]
            if run.replans == 0 and not any("downstream" in h for h in (run.plan or {}).get("hypotheses", [])):
                return self._call("submit_plan", {
                    "hypotheses": [f"downstream degradation: {d} dominates {s} latency", "saturation", "bad deploy"],
                    "steps": [f"state and logs of {d}", "compare deploy time with symptom onset"]},
                    f"Replan: traces show {d} accounts for {top['share_of_root_p99']:.0%} of {s} p99.")
            if not self._find(run, "get_resource_state", service=d):
                return self._call("get_resource_state", {"service": d}, f"What kind of workload is {d}?")
            if not self._find(run, "search_logs", service=d):
                return self._call("search_logs", {"service": d, "window_minutes": RECENT_WINDOW_MIN, "limit": LOG_CLUSTERS},
                                  f"Is {d} itself unhealthy (locks, slow queries)?")
        return self._diagnose(run, s)

    def _diagnose(self, run: RunState, s: str) -> LLMResponse:
        hist = self._find(run, "get_deployment_history", service=s)
        err = self._find(run, "query_metrics", metric="error_ratio", service=s)
        lat = self._find(run, "query_metrics", metric="latency_p99_seconds", service=s)
        thr = self._find(run, "query_metrics", metric="cpu_throttle_ratio", service=s)
        traces = self._find(run, "get_traces", service=s)
        errlogs = self._find(run, "search_logs", service=s, level="ERROR")
        state = self._find(run, "get_resource_state", service=s)
        revs = hist.result["revisions"] if hist else []
        cur = next((r for r in revs if r["current"]), None)
        prev = next((r for r in revs if cur and r["revision"] < cur["revision"] and r["version"] != cur["version"]), None)

        def onset(ev: Evidence | None) -> float | None:
            return ev.result.get("minutes_since_change") if ev else None

        # Rule 1: code defect shipped by the latest revision
        err_onset = onset(err)
        new_version_errors = bool(errlogs and cur and any(
            x.get("version") == cur["version"] for c in errlogs.result.get("clusters", []) for x in c["exemplars"]))
        if (err and err.result["current"] > ERROR_RATIO_INCIDENT and err_onset is not None and cur and prev
                and 0 <= cur["age_minutes"] - err_onset <= SUSPECT_DEPLOY_WINDOW_MIN and new_version_errors):
            ev = [hist.evidence_id, err.evidence_id, errlogs.evidence_id]  # type: ignore[union-attr]
            return self._call("submit_diagnosis", {
                "root_cause": f"{s} {cur['version']} (revision {cur['revision']}, '{cur['change_cause']}') introduced "
                              f"errors: 5xx ratio {err.result['baseline']:.1%} -> {err.result['current']:.1%} starting "
                              f"{cur['age_minutes'] - err_onset:.1f} min after rollout; exceptions carry version "
                              f"{cur['version']}.",
                "root_service": s, "category": "bad_deploy", "confidence": CONFIDENCE["bad_deploy"], "evidence_ids": ev,
                "recommended_action": "rollback"},
                f"Temporal correlation + version-tagged exceptions. Previous good revision {prev['revision']} "
                f"({prev['version']}).")
        # Rule 2: latency dominated by a downstream dependency
        top = (traces.result.get("downstream") or [None])[0] if traces else None
        if top and top["share_of_root_p99"] >= DOMINANT_DOWNSTREAM_SHARE and lat and lat.result["current"] > LATENCY_INCIDENT_S:
            d = top["peer.service"]
            dstate = self._find(run, "get_resource_state", service=d)
            dlogs = self._find(run, "search_logs", service=d)
            ev = [e.evidence_id for e in (traces, lat, dstate, dlogs) if e]
            deploy_note = ""
            if cur and onset(lat) is not None:
                gap = cur["age_minutes"] - onset(lat)  # type: ignore[operator]
                deploy_note = (f" The recent {s} deploy ({cur['version']}, '{cur['change_cause']}') is not the cause:"
                               f" no new errors, and {s}'s own span time is unchanged (gap {gap:.1f} min is coincidental).")
            kind = dstate.result["kind"] if dstate else "unknown"
            owner = ", ".join(dstate.result.get("owners", [])) if dstate else "owning team"
            return self._call("submit_diagnosis", {
                "root_cause": f"{d} ({kind}) dominates {s} latency: {top['share_of_root_p99']:.0%} of p99 "
                              f"({top['p99_ms']:.0f} ms); logs show lock waits/slow queries.{deploy_note} "
                              f"Page {owner}.",
                "root_service": d, "category": "dependency_failure", "confidence": CONFIDENCE["dependency_failure"], "evidence_ids": ev,
                "recommended_action": "none"},
                f"Stateful dependency {d} is the bottleneck; rolling back or restarting {s} would not help.")
        # Rule 3: CPU saturation
        if thr and thr.result["current"] > THROTTLE_SATURATED and state and state.result.get("hpa"):
            reps = state.result["replicas"]
            want = min(state.result["hpa"]["maxReplicas"], max(reps + 1, math.ceil(reps * SCALE_FACTOR)))
            return self._call("submit_diagnosis", {
                "root_cause": f"{s} is CPU-saturated: throttled {thr.result['current']:.0%} of CFS periods "
                              f"(baseline {thr.result['baseline']:.0%}) after a traffic increase; no deploy correlates. "
                              f"Scale {reps} -> {want} replicas via HPA minReplicas.",
                "root_service": s, "category": "saturation", "confidence": CONFIDENCE["saturation"],
                "evidence_ids": [thr.evidence_id, lat.evidence_id if lat else thr.evidence_id, state.evidence_id],
                "recommended_action": "scale"},
                "Throttling explains the latency; capacity, not code.")
        ev = [e.evidence_id for e in (err, lat, thr) if e] or list(run.evidence)[:1]
        return self._call("submit_diagnosis", {
            "root_cause": "No rule matched with sufficient evidence; handing to on-call.", "root_service": s,
            "category": "unknown", "confidence": CONFIDENCE["unknown"], "evidence_ids": ev, "recommended_action": "none"},
            "Low confidence.")

    # ------------------------------------------------------------------ propose
    def _propose(self, run: RunState) -> LLMResponse:
        d = run.diagnosis or {}
        ev = d.get("evidence_ids", [])
        s = d.get("root_service", run.root_service)
        act = d.get("recommended_action")
        if act == "rollback":
            hist = self._find(run, "get_deployment_history", service=s)
            revs = hist.result["revisions"] if hist else []
            cur = next(r for r in revs if r["current"])
            prev = next(r for r in revs if r["revision"] < cur["revision"] and r["version"] != cur["version"])
            return self._call("propose_rollback", {"deployment": s, "to_revision": prev["revision"], "evidence_ids": ev,
                                                   "rationale": f"roll back {cur['version']} -> {prev['version']}"},
                              f"Diagnosis is bad_deploy; revision {prev['revision']} ({prev['version']}) is the last "
                              "known-good revision.")
        if act == "scale":
            st = self._find(run, "get_resource_state", service=s)
            reps = st.result["replicas"] if st else 1
            hpa_max = st.result["hpa"]["maxReplicas"] if st else reps
            want = min(hpa_max, max(reps + 1, math.ceil(reps * SCALE_FACTOR)))
            return self._call("propose_scale", {"deployment": s, "replicas": want, "evidence_ids": ev,
                                                "rationale": f"relieve CPU throttling: {reps} -> {want}"},
                              f"Saturation: doubling replicas within the HPA max ({hpa_max}) should clear throttling.")
        return self._call("no_action", {"reason": d.get("root_cause", "insufficient evidence")[:NO_ACTION_REASON_CHARS]},
                          "Automation would not help; report to the owning team.")
