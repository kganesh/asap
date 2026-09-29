"""The agent orchestrator: a bounded state machine around an untrusted planner.

    TRIAGE -> PLAN -> INVESTIGATE (ReAct over read tools, replan <= 2) -> DIAGNOSE -> PROPOSE
           -> POLICY_CHECK -> [AWAIT_APPROVAL] -> EXECUTE (re-validate) -> VERIFY -> RESOLVED | REVERTED
    any cap, deadline, LLM outage or deny -> REPORT_ONLY

The LLM chooses what to look at and what to propose. Code chooses the next state and whether
anything happens. Every transition is persisted and audited before its side effect.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .. import __version__
from ..audit.log import AuditLog
from ..console import NullUI
from ..control.approval import ApprovalGate, state_hash
from ..control.plane import ControlPlane, Controls
from ..control.policy import PolicyEngine
from ..control.store import StateStore, StoreUnavailable
from ..executor.executor import Executor, PreconditionFailed
from ..ingest.pipeline import Incident
from ..models import Proposal, RunState, new_id
from ..report import write_report
from ..simclient import SimClient
from ..telemetry import metrics as m
from ..telemetry.tracing import setup as tracer_setup
from ..telemetry.tracing import write_spans_to
from ..tools.gateway import ToolGateway, ToolRejected
from ..tools.schemas import ACTION_TOOLS, ACTION_TYPE, READ_TOOLS, TOOL_SCHEMA_VERSION, tool_specs
from . import prompts
from . import states as S
from .llm import LLM, LLMUnavailable

INVESTIGATE_TOOLS = list(READ_TOOLS) + ["submit_plan", "submit_diagnosis"]


class DeadlineExceeded(Exception):
    pass


class Orchestrator:
    def __init__(self, sim_url: str, llm: LLM, store: StateStore, policy: PolicyEngine, approval: ApprovalGate,
                 runs_dir: Path, ui: object | None = None, controls: Controls | None = None,
                 limits: S.Limits | None = None) -> None:
        self.llm = llm
        self.store = store
        self.policy = policy
        self.approval = approval
        self.runs_dir = runs_dir
        self.ui = ui or NullUI()
        self.limits = limits or S.Limits()
        self.gateway = ToolGateway(sim_url)
        self.executor = Executor(sim_url, store)
        self.plane = ControlPlane(sim_url, store, self.executor, policy, controls)
        self.sim = SimClient.reader(sim_url)
        self.tracer = tracer_setup()

    # ================================================================== entry point
    def run(self, incident: Incident) -> RunState:
        run = RunState(new_id("run"), incident.incident_id, incident.root_service, incident.services,
                       incident.alerts, f"{self.llm.name}:{self.llm.model}")
        versions = {"asap": __version__, "model": self.llm.model, "prompt": prompts.PROMPT_VERSION,
                    "tool_schema": TOOL_SCHEMA_VERSION, "policy_bundle": self.policy.digest,
                    "policy_engine": self.policy.name}
        audit = AuditLog(self.runs_dir, run.run_id, versions)
        write_spans_to(audit.dir / "spans.jsonl")
        self._audit, self._run = audit, run
        self._deadline = time.time() + self.limits.deadline_s
        self._messages: list[dict] = []
        self.ui.start(run, incident)
        with self.tracer.start_as_current_span("asap.incident", attributes={
                "asap.run_id": run.run_id, "asap.incident_id": incident.incident_id,
                "asap.root_service": incident.root_service, "asap.llm": run.llm_name}) as span:
            self._log("system", "run_started", {"incident": _incident_dict(incident), "limits": vars(self.limits)})
            try:
                self._drive(run, incident)
            except DeadlineExceeded:
                self._to(S.REPORT_ONLY, f"run deadline of {self.limits.deadline_s:.0f}s exceeded; "
                                        "reporting evidence gathered so far")
            except LLMUnavailable as e:
                self._to(S.REPORT_ONLY, f"LLM unavailable ({e}); degraded to report-only (paging is unaffected)")
            except StoreUnavailable as e:
                if S.REPORT_ONLY in S.ALLOWED.get(run.state, set()):
                    self._to(S.REPORT_ONLY, f"{e}: failing closed")
            finally:
                self._finish(run, incident, span)
        return run

    # ================================================================== state machine
    def _to(self, dst: str, reason: str = "") -> None:
        run = self._run
        S.check(run.state, dst)
        src = run.state
        run.state = dst
        run.transitions.append((src, dst, reason))
        if dst in S.TERMINAL:
            run.outcome, run.outcome_reason = dst, reason
        self.store.save_run(run.run_id, run.incident_id, dst, self._sim_now(), run.to_dict())  # persisted first
        self._log("system", "transition", {"from": src, "to": dst, "reason": reason})
        self.ui.transition(run, src, dst, reason)

    def _drive(self, run: RunState, incident: Incident) -> None:
        # ---------------------------------------------------------- TRIAGE
        with self._span("asap.state.TRIAGE"):
            if not self._still_firing(run):
                return self._to(S.REPORT_ONLY, "alerts resolved before investigation started")
            if not self.store.acquire_lease(f"incident:{run.incident_id}", run.run_id, self._sim_now()):
                return self._to(S.REPORT_ONLY, "another run already holds this incident (duplicate delivery)")
            hints = self._runbook_hints(run)
        brief = _incident_dict(incident) | {"runbook_signature_candidates (unconfirmed)": hints}
        self._messages.append({"role": "user", "text": prompts.incident_brief(brief)})
        self._to(S.PLAN, "incident admitted")

        # ---------------------------------------------------------- PLAN + INVESTIGATE (ReAct)
        resp = self._ask(S.PLAN, ["submit_plan"])
        self._record_plan(resp.tool_args, resp)
        self._to(S.INVESTIGATE, "plan recorded")
        invalid_diagnoses = 0
        while True:
            if run.steps >= self.limits.max_steps:
                return self._to(S.REPORT_ONLY, f"investigation step cap ({self.limits.max_steps}) reached")
            resp = self._ask(S.INVESTIGATE, INVESTIGATE_TOOLS)
            run.steps += 1
            name, args = resp.tool_name, resp.tool_args
            if name in READ_TOOLS:
                self._read_tool(resp)
            elif name == "submit_plan":
                if run.replans >= self.limits.max_replans:
                    self._tool_error(resp, f"replan limit ({self.limits.max_replans}) reached; diagnose now")
                    continue
                run.replans += 1
                self._to(S.PLAN, f"replan {run.replans}: leading hypothesis refuted")
                self._record_plan(args, resp)
                self._to(S.INVESTIGATE, "revised plan recorded")
            elif name == "submit_diagnosis":
                try:
                    diag = self.gateway.validate(name, args).model_dump()  # type: ignore[attr-defined]
                except ToolRejected as e:
                    diag, err = None, str(e)
                else:
                    unknown = [e for e in diag["evidence_ids"] if e not in run.evidence]
                    err = f"evidence_ids never issued in this run: {unknown}" if unknown else ""
                if err:
                    invalid_diagnoses += 1
                    m.REJECTED_TOOL_CALLS.labels("invalid_diagnosis").inc()
                    self._log("system", "diagnosis_rejected", {"error": err})
                    if invalid_diagnoses >= self.limits.max_invalid_diagnoses:
                        return self._to(S.REPORT_ONLY, "diagnosis repeatedly cited evidence that does not exist")
                    self._tool_error(resp, err)
                    continue
                run.diagnosis = diag
                m.TIME_TO_DIAGNOSIS.observe(time.time() - run.started_wall)
                self._log("agent", "diagnosis", diag)
                self._tool_ok(resp, {"status": "diagnosis recorded"}, prompts.PHASE_PROPOSE)
                self._to(S.DIAGNOSE, f"{diag['category']} (confidence {diag['confidence']:.2f})")
                break
            else:
                reason = "not available in this phase" if name in ACTION_TOOLS else "unknown tool"
                m.REJECTED_TOOL_CALLS.labels(reason.replace(" ", "_")).inc()
                self._tool_error(resp, f"tool '{name}' rejected: {reason}. The action space is closed.")

        # ---------------------------------------------------------- DIAGNOSE -> PROPOSE
        if not self._still_firing(run):
            return self._to(S.REPORT_ONLY, "alerts self-resolved before a proposal was made")
        self._to(S.PROPOSE, "diagnosis accepted")
        proposal = None
        for _attempt in range(2):
            resp = self._ask(S.PROPOSE, list(ACTION_TOOLS))
            if resp.tool_name == "no_action":
                self._log("agent", "no_action", resp.tool_args)
                return self._to(S.REPORT_ONLY, "agent recommends no automated action; report routed to the "
                                               "owning team (see diagnosis)")
            try:
                if resp.tool_name not in ACTION_TOOLS:
                    raise ToolRejected(f"'{resp.tool_name}' is not an action tool")
                parsed = self.gateway.validate(resp.tool_name, resp.tool_args)
            except ToolRejected as e:
                m.REJECTED_TOOL_CALLS.labels("invalid_proposal").inc()
                self._tool_error(resp, str(e))
                continue
            p = parsed.model_dump()  # type: ignore[attr-defined]
            target = p.pop("deployment", None) or p.pop("cache", None)
            proposal = Proposal(new_id("prop"), run.run_id, run.incident_id, ACTION_TYPE[resp.tool_name], target,
                                {k: v for k, v in p.items() if k not in ("evidence_ids", "rationale")},
                                p["evidence_ids"], p["rationale"], float((run.diagnosis or {}).get("confidence", 0)))
            self._tool_ok(resp, {"proposal_id": proposal.proposal_id,
                                 "status": "submitted to the control plane; the agent's part is complete"})
            break
        if proposal is None:
            return self._to(S.REPORT_ONLY, "no valid proposal after 2 attempts")
        run.proposal = proposal
        self._log("agent", "proposal", vars(proposal))
        self._to(S.POLICY_CHECK, f"{proposal.action} {proposal.target} {proposal.params}")
        self._policy_to_execution(run, proposal)

    # ================================================================== deterministic tail
    def _policy_to_execution(self, run: RunState, p: Proposal) -> None:
        reevaluations = 0
        while True:
            with self._span("asap.policy.eval", {"asap.action": p.action, "asap.target": p.target}) as sp:
                d = self.plane.submit(run, p)
                sp.set_attribute("asap.verdict", d.verdict)
                sp.set_attribute("asap.tier", d.tier)
            run.decision = d
            m.ACTIONS.labels(p.action, str(d.tier), d.verdict).inc()
            for r in d.reasons if d.verdict == "deny" else []:
                m.DENIALS.labels(r[:60]).inc()
            self._log("policy", "decision", {"verdict": d.verdict, "tier": d.tier, "reasons": d.reasons,
                                             "blast_radius": d.blast_radius, "engine": d.policy_engine,
                                             "dry_run": vars(d.dry_run) if d.dry_run else None,
                                             "input": d.policy_input})
            self.ui.decision(run, d)
            if d.verdict == "deny":
                return self._to(S.REPORT_ONLY, "policy denied: " + "; ".join(d.reasons))
            if d.verdict == "require_approval":
                self._to(S.AWAIT_APPROVAL, "; ".join(d.reasons))
                owners = d.policy_input["target"].get("owners") or []
                packet = self._approval_packet(run, p, d)
                with self._span("asap.approval", {"asap.required": d.required_approvals}) as sp:
                    res = self.approval.request(packet, owners, d.required_approvals)
                    sp.set_attribute("asap.approved", res["approved"])
                run.approval = res
                self._log("human", "approval", res)
                self.ui.approval(run, res)
                if not res["approved"]:
                    return self._to(S.REPORT_ONLY, f"not approved: {res['reason']}")
                self._to(S.EXECUTE, f"approved by {', '.join(res['approvers'])}")
            else:
                self._to(S.EXECUTE, "tier 1: within auto-remediation bounds")

            # ---------------------------------------------------------- EXECUTE
            if not self._still_firing(run):
                return self._to(S.REPORT_ONLY, "alerts self-resolved before execution; nothing to do")
            ok, why, fresh = self.plane.revalidate(run, p, d)
            self._log("policy", "revalidation", {"ok": ok, "reason": why, "fresh_verdict": fresh.verdict})
            if not ok:
                if reevaluations >= self.limits.max_reevaluations:
                    return self._to(S.REPORT_ONLY, f"re-validation failed again: {why}")
                reevaluations += 1
                self._to(S.POLICY_CHECK, why)
                continue
            if not self.store.acquire_lease(f"target:{p.target}", run.run_id, self._sim_now()):
                return self._to(S.REPORT_ONLY, f"another actor holds the lease on {p.target}")
            try:
                self.store.record_action(p.proposal_id, run.run_id, p.target, p.action, self._sim_now())
                with self._span("asap.execute", {"asap.action": p.action, "asap.target": p.target}):
                    try:
                        ex = self.executor.execute(p, d.dry_run.precondition_resource_version if d.dry_run else None)
                    except PreconditionFailed as e:
                        self.store.set_outcome(p.proposal_id, "aborted_precondition")
                        if reevaluations >= self.limits.max_reevaluations:
                            return self._to(S.REPORT_ONLY, str(e))
                        reevaluations += 1
                        self._to(S.POLICY_CHECK, str(e))
                        continue
                    except Exception as e:  # noqa: BLE001 - crash mid-apply: reconcile from observed state
                        rec = self.executor.recover(p)
                        self._log("executor", "crash_recovered", {"error": str(e), "reconciled": rec})
                        if not rec or not rec.get("observed_applied"):
                            self.store.set_outcome(p.proposal_id, "failed")
                            return self._to(S.REPORT_ONLY, f"execution failed ({e}); escalated")
                        ex = {"applied": True, "reconciled_after_crash": True, "previous": {}}
            finally:
                self.store.release_lease(f"target:{p.target}", run.run_id)
            run.execution = ex
            self._log("executor", "applied", ex)
            self.ui.execution(run, ex)
            self._to(S.VERIFY, "applied; waiting for SLI recovery")
            return self._verify(run, p, ex)

    def _verify(self, run: RunState, p: Proposal, ex: dict) -> None:
        names = {a["labels"]["alertname"] for a in run.alerts if a["labels"]["service"] == run.root_service}
        with self._span("asap.verify"):
            v = self.executor.verify(run.root_service, names)
        run.verification = v
        self._log("executor", "verification", v)
        self.ui.verification(run, v)
        if v["recovered"]:
            self.store.set_outcome(p.proposal_id, "improved")
            return self._to(S.RESOLVED, f"SLIs recovered after {v['waited_minutes']} min")
        self.store.set_outcome(p.proposal_id, "no_improvement")
        rev = self.executor.revert(p, ex) if ex.get("previous") else {"reverted": False, "rule": "no prior state"}
        m.REVERTS.labels(p.action).inc()
        self._log("executor", "revert", rev)
        run.verification["revert"] = rev
        self._to(S.REVERTED, f"no recovery; {rev['rule']}; paging on-call")

    # ================================================================== LLM plumbing
    def _ask(self, phase: str, tools: list[str]):  # type: ignore[no-untyped-def]
        remaining = self._deadline - time.time()
        if remaining <= 0:
            raise DeadlineExceeded()
        run = self._run
        with self._span("gen_ai.chat", {"gen_ai.system": self.llm.name, "gen_ai.request.model": self.llm.model,
                                        "asap.phase": phase}) as sp:
            resp = self.llm.next(prompts.SYSTEM, self._messages, tool_specs(tools), run, phase,
                                 min(self.limits.llm_call_timeout_s, remaining))
            sp.set_attribute("gen_ai.usage.input_tokens", resp.tokens_in)
            sp.set_attribute("gen_ai.usage.output_tokens", resp.tokens_out)
            sp.set_attribute("gen_ai.response.tool", resp.tool_name)
        run.tokens_in += resp.tokens_in
        run.tokens_out += resp.tokens_out
        m.TOKENS.labels(resp.model or self.llm.model, "in").inc(resp.tokens_in)
        m.TOKENS.labels(resp.model or self.llm.model, "out").inc(resp.tokens_out)
        self._messages.append({"role": "assistant", "text": resp.thought,
                               "tool_call": {"id": resp.tool_id, "name": resp.tool_name, "args": resp.tool_args}})
        self._log("agent", "llm_turn", {"phase": phase, "tool": resp.tool_name, "args": resp.tool_args,
                                        "thought": resp.thought, "tokens_in": resp.tokens_in,
                                        "tokens_out": resp.tokens_out, "latency_ms": round(resp.latency_ms, 1)})
        self.ui.llm(run, phase, resp)
        return resp

    def _record_plan(self, args: dict, resp) -> None:  # type: ignore[no-untyped-def]
        try:
            plan = self.gateway.validate("submit_plan", args).model_dump()  # type: ignore[attr-defined]
        except ToolRejected as e:
            plan = {"hypotheses": ["(invalid plan)"], "steps": [], "error": str(e)}
        self._run.plan = plan
        self._log("agent", "plan", plan)
        left = self.limits.max_steps - self._run.steps
        self._tool_ok(resp, {"status": "plan recorded"}, prompts.PHASE_INVESTIGATE.format(steps=left))

    def _read_tool(self, resp) -> None:  # type: ignore[no-untyped-def]
        with self._span(f"asap.tool.{resp.tool_name}", {"asap.args": json.dumps(resp.tool_args)}) as sp:
            try:
                r = self.gateway.run_read(self._run, resp.tool_name, resp.tool_args)
            except ToolRejected as e:
                m.REJECTED_TOOL_CALLS.labels("read_rejected").inc()
                sp.set_attribute("asap.rejected", str(e)[:200])
                self._tool_error(resp, str(e))
                return
            sp.set_attribute("asap.evidence_id", r.evidence_id or "")
        self._log("agent", "tool_result", {"tool": resp.tool_name, "evidence_id": r.evidence_id,
                                           "result": r.content})
        self.ui.tool(self._run, resp.tool_name, resp.tool_args, r.content)
        self._tool_ok(resp, r.content)

    def _tool_ok(self, resp, content: dict, suffix: str = "") -> None:  # type: ignore[no-untyped-def]
        text = json.dumps(content, default=str)
        self._messages.append({"role": "tool", "tool_call_id": resp.tool_id, "name": resp.tool_name,
                               "content": text + ("\n\n" + suffix if suffix else ""), "is_error": False})

    def _tool_error(self, resp, err: str) -> None:  # type: ignore[no-untyped-def]
        self._log("system", "tool_rejected", {"tool": resp.tool_name, "args": resp.tool_args, "error": err})
        self.ui.rejected(self._run, resp.tool_name, err)
        self._messages.append({"role": "tool", "tool_call_id": resp.tool_id, "name": resp.tool_name,
                               "content": json.dumps({"error": err}), "is_error": True})

    # ================================================================== helpers
    def _still_firing(self, run: RunState) -> bool:
        try:
            firing = {a["labels"]["service"] for a in self.sim.alerts()}
        except Exception:  # noqa: BLE001 - if we can't tell, assume still firing (humans are paged anyway)
            return True
        return bool(firing & set(run.services))

    def _runbook_hints(self, run: RunState) -> list[str]:
        hints = []
        try:
            for r in self.sim.history(run.root_service):
                if r["current"] and r["age_minutes"] <= 15:
                    hints.append(f"RB-017 bad-deploy signature: {run.root_service} revision {r['revision']} "
                                 f"deployed {r['age_minutes']} min ago")
        except Exception:  # noqa: BLE001
            pass
        return hints

    def _approval_packet(self, run: RunState, p: Proposal, d) -> dict:  # type: ignore[no-untyped-def]
        rv = d.dry_run.precondition_resource_version if d.dry_run else None
        cited = [run.evidence[e] for e in p.evidence_ids if e in run.evidence]
        return {"incident": run.incident_id, "run": run.run_id, "diagnosis": run.diagnosis,
                "proposal": {"action": p.action, "target": p.target, "params": p.params, "rationale": p.rationale},
                "dry_run_diff": d.dry_run.diff if d.dry_run else {}, "policy_reasons": d.reasons,
                "blast_radius": d.blast_radius, "required_approvals": d.required_approvals,
                "evidence": [{"id": e.evidence_id, "tool": e.tool, "args": e.args} for e in cited],
                "plan": run.plan, "state_hash": state_hash(p.target, rv, p.params)}

    def _sim_now(self) -> float:
        try:
            return self.sim.now()
        except Exception:  # noqa: BLE001
            return time.time()

    def _log(self, actor: str, event: str, payload: dict) -> None:
        try:
            self._audit.append(actor, event, payload, self._sim_now())
        except OSError:
            pass

    def _span(self, name: str, attrs: dict | None = None):  # type: ignore[no-untyped-def]
        return self.tracer.start_as_current_span(name, attributes=attrs or {})

    def _finish(self, run: RunState, incident: Incident, span) -> None:  # type: ignore[no-untyped-def]
        if run.state not in S.TERMINAL:
            run.state, run.outcome = S.REPORT_ONLY, S.REPORT_ONLY
            run.outcome_reason = run.outcome_reason or "run ended unexpectedly"
        try:
            self.store.release_lease(f"incident:{run.incident_id}", run.run_id)
            self.store.save_run(run.run_id, run.incident_id, run.state, self._sim_now(), run.to_dict())
        except StoreUnavailable:
            pass
        span.set_attribute("asap.outcome", run.outcome or "")
        span.set_attribute("gen_ai.usage.input_tokens", run.tokens_in)
        span.set_attribute("gen_ai.usage.output_tokens", run.tokens_out)
        self._log("system", "run_finished", {"outcome": run.outcome, "reason": run.outcome_reason,
                                             "steps": run.steps, "tokens_in": run.tokens_in,
                                             "tokens_out": run.tokens_out,
                                             "untrusted_strings_redacted": len(run.flagged_untrusted)})
        self._audit.anchor()
        m.RUNS.labels(run.outcome or "unknown").inc()
        report = write_report(self._audit.dir, run, incident)
        m.dump(self.runs_dir / "metrics.prom")
        self.ui.final(run, report)


def _incident_dict(i: Incident) -> dict:
    return {"incident_id": i.incident_id, "severity": i.severity, "root_service_candidate": i.root_service,
            "affected_services": i.services, "cluster": i.cluster, "cell": i.cell,
            "alerts": [{"alertname": a["labels"]["alertname"], "service": a["labels"]["service"],
                        "severity": a["labels"].get("severity"), "startsAt": a["startsAt"],
                        "description": a.get("annotations", {}).get("description", "")} for a in i.alerts]}
