"""The agent orchestrator: a bounded state machine around an untrusted planner.

    TRIAGE -> PLAN -> INVESTIGATE (ReAct over read tools, replan <= 2) -> DIAGNOSE -> PROPOSE
           -> POLICY_CHECK -> [AWAIT_APPROVAL] -> EXECUTE (lease, re-validate) -> VERIFY -> RESOLVED | REVERTED
    any cap, deadline, LLM outage, deny or unexpected error -> REPORT_ONLY

The LLM chooses what to look at and what to propose. Code chooses the next state and whether
anything happens. Every transition is persisted and audited before its side effect.

Failure policy: detection-side gates may proceed when they cannot read state (humans are paged
anyway); execution-side gates fail closed.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from .. import __version__
from ..audit.log import AuditLog
from ..console import NullUI
from ..control.approval import ApprovalGate, state_hash
from ..control.plane import ControlPlane, Controls
from ..control.policy import PolicyEngine
from ..control.store import StateStore, StoreUnavailable
from ..executor.executor import Executor, PreconditionFailed
from ..ingest.pipeline import Incident
from ..models import Decision, Proposal, RunState, new_id
from ..report import write_report
from ..simclient import SimClient
from ..telemetry import metrics as m
from ..telemetry.tracing import register_run, unregister_run
from ..telemetry.tracing import setup as tracer_setup
from ..tools.gateway import ToolGateway, ToolRejected
from ..tools.schemas import ACTION_TOOLS, ACTION_TYPE, READ_TOOLS, TOOL_SCHEMA_VERSION, tool_specs
from . import prompts
from . import states as S
from .context import ContextManager, TokenBudgetExceeded, estimate_tokens
from .llm import LLM, LLMResponse, LLMUnavailable

log = logging.getLogger(__name__)

INVESTIGATE_TOOLS = list(READ_TOOLS) + ["submit_plan", "submit_diagnosis"]
MAX_PLAN_ATTEMPTS = 2
MAX_PROPOSAL_ATTEMPTS = 2


class DeadlineExceeded(Exception):
    pass


class Orchestrator:
    def __init__(self, sim_url: str, llm: LLM, store: StateStore, policy: PolicyEngine, approval: ApprovalGate,
                 runs_dir: Path, ui: Any | None = None, controls: Controls | None = None,
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
        """Run one incident to a terminal state. Never raises: any failure ends as REPORT_ONLY."""
        run = RunState(new_id("run"), incident.incident_id, incident.root_service, incident.services,
                       incident.alerts, f"{self.llm.name}:{self.llm.model}")
        versions = {"asap": __version__, "model": self.llm.model, "prompt": prompts.PROMPT_VERSION,
                    "tool_schema": TOOL_SCHEMA_VERSION, "policy_bundle": self.policy.digest,
                    "policy_engine": self.policy.name}
        audit = AuditLog(self.runs_dir, run.run_id, versions)
        self._audit, self._run = audit, run
        self._deadline = time.time() + self.limits.deadline_s
        self._messages: list[dict] = []
        self._services: list[str] = []
        self._ctx = ContextManager(self.limits.compact_at_tokens, self.limits.keep_recent_results,
                                   self.limits.max_run_input_tokens)
        self.ui.start(run, incident)
        with self.tracer.start_as_current_span("asap.incident", attributes={
                "asap.run_id": run.run_id, "asap.incident_id": incident.incident_id,
                "asap.root_service": incident.root_service, "asap.llm": run.llm_name}) as span:
            trace_id = span.get_span_context().trace_id
            register_run(trace_id, audit.dir / "spans.jsonl")
            self._log("system", "run_started", {"incident": _incident_dict(incident), "limits": vars(self.limits)})
            try:
                self._drive(run, incident)
            except DeadlineExceeded:
                self._abort(f"run deadline of {self.limits.deadline_s:.0f}s exceeded; reporting evidence gathered so far")
            except TokenBudgetExceeded as e:
                log.warning("token budget exhausted in run %s: %s", run.run_id, e)
                self._abort(f"{e}; reporting evidence gathered so far")
            except LLMUnavailable as e:
                log.warning("LLM unavailable in run %s: %s", run.run_id, e)
                self._abort(f"LLM unavailable ({e}); degraded to report-only (paging is unaffected)")
            except StoreUnavailable as e:
                log.error("state store unavailable in run %s: %s", run.run_id, e)
                self._abort(f"{e}: failing closed")
            except Exception as e:  # noqa: BLE001 - last line of defence: record, never crash the caller
                log.exception("unexpected error in run %s (state %s)", run.run_id, run.state)
                self._abort(f"unexpected error in {run.state} ({type(e).__name__}: {e}); failing closed, escalated")
            finally:
                self._finish(run, incident, span)
        unregister_run(trace_id)
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
        try:
            self.store.save_run(run.run_id, run.incident_id, dst, self._sim_now_or_wall(), run.to_dict())
        except Exception:  # noqa: BLE001 - persistence failure must not hide the transition
            log.exception("failed to persist transition %s -> %s for %s", src, dst, run.run_id)
        self._log("system", "transition", {"from": src, "to": dst, "reason": reason})
        self.ui.transition(run, src, dst, reason)

    def _abort(self, reason: str) -> None:
        """Move to REPORT_ONLY from wherever we are, if the run isn't already terminal."""
        if self._run.state in S.TERMINAL:
            return
        self._to(S.REPORT_ONLY, reason)

    def _drive(self, run: RunState, incident: Incident) -> None:
        # ---------------------------------------------------------- TRIAGE
        with self._span("asap.state.TRIAGE"):
            if self._firing(run) is False:  # unknown -> proceed: this gate is detection-side
                return self._to(S.REPORT_ONLY, "alerts resolved before investigation started")
            if not self.store.acquire_lease(f"incident:{run.incident_id}", run.run_id, self._clock()):
                return self._to(S.REPORT_ONLY, "another run already holds this incident (duplicate delivery)")
            self._services = self._catalog()
            hints = self._runbook_hints(run)
        brief = _incident_dict(incident) | {"runbook_signature_candidates (unconfirmed)": hints,
                                            "known_services": self._services}
        self._messages.append({"role": "user", "text": prompts.incident_brief(brief)})
        self._to(S.PLAN, "incident admitted")

        # ---------------------------------------------------------- PLAN
        for _ in range(MAX_PLAN_ATTEMPTS):
            resp = self._ask(S.PLAN, ["submit_plan"])
            if self._record_plan(resp):
                break
        else:
            return self._to(S.REPORT_ONLY, f"no valid plan after {MAX_PLAN_ATTEMPTS} attempts")
        self._to(S.INVESTIGATE, "plan recorded")

        # ---------------------------------------------------------- INVESTIGATE (bounded ReAct)
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
                if not self.gateway.is_valid("submit_plan", args):
                    self._record_plan(resp)  # feeds the validation error back; plan unchanged
                    continue
                run.replans += 1
                self._to(S.PLAN, f"replan {run.replans}: leading hypothesis refuted")
                self._record_plan(resp)
                self._to(S.INVESTIGATE, "revised plan recorded")
            elif name == "submit_diagnosis":
                diag, err = self._validate_diagnosis(run, args)
                if err:
                    invalid_diagnoses += 1
                    m.REJECTED_TOOL_CALLS.labels("invalid_diagnosis").inc()
                    self._log("system", "diagnosis_rejected", {"error": err})
                    if invalid_diagnoses >= self.limits.max_invalid_diagnoses:
                        return self._to(S.REPORT_ONLY, "diagnosis repeatedly failed validation: " + err[:160])
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
        if self._firing(run) is False:
            return self._to(S.REPORT_ONLY, "alerts self-resolved before a proposal was made")
        self._to(S.PROPOSE, "diagnosis accepted")
        proposal = self._propose(run)
        if proposal is None:
            return None  # _propose already moved to REPORT_ONLY
        run.proposal = proposal
        self._log("agent", "proposal", vars(proposal))
        self._to(S.POLICY_CHECK, f"{proposal.action} {proposal.target} {proposal.params}")
        self._policy_to_execution(run, proposal)

    def _validate_diagnosis(self, run: RunState, args: dict) -> tuple[dict | None, str]:
        try:
            diag = self.gateway.validate("submit_diagnosis", args).model_dump()  # type: ignore[attr-defined]
        except ToolRejected as e:
            return None, str(e)
        unknown = [e for e in diag["evidence_ids"] if e not in run.evidence]
        if unknown:
            return None, f"evidence_ids never issued in this run: {unknown}"
        return diag, ""

    def _propose(self, run: RunState) -> Proposal | None:
        for _ in range(MAX_PROPOSAL_ATTEMPTS):
            resp = self._ask(S.PROPOSE, list(ACTION_TOOLS))
            if resp.tool_name == "no_action":
                self._log("agent", "no_action", resp.tool_args)
                self._to(S.REPORT_ONLY, "agent recommends no automated action; report routed to the owning team "
                                        "(see diagnosis)")
                return None
            try:
                if resp.tool_name not in ACTION_TOOLS:
                    raise ToolRejected(f"'{resp.tool_name}' is not an action tool")
                p = self.gateway.validate(resp.tool_name, resp.tool_args).model_dump()  # type: ignore[attr-defined]
            except ToolRejected as e:
                m.REJECTED_TOOL_CALLS.labels("invalid_proposal").inc()
                self._tool_error(resp, str(e))
                continue
            target = p.pop("deployment", None) or p.pop("cache", None)
            meta = {k: p.pop(k) for k in ("evidence_ids", "rationale", "reasoning")}
            proposal = Proposal(new_id("prop"), run.run_id, run.incident_id, ACTION_TYPE[resp.tool_name], target,
                                p, meta["evidence_ids"], meta["rationale"],
                                float((run.diagnosis or {}).get("confidence", 0)))
            self._tool_ok(resp, {"proposal_id": proposal.proposal_id,
                                 "status": "submitted to the control plane; the agent's part is complete"})
            return proposal
        self._to(S.REPORT_ONLY, f"no valid proposal after {MAX_PROPOSAL_ATTEMPTS} attempts")
        return None

    # ================================================================== deterministic tail
    def _decide(self, run: RunState, p: Proposal) -> Decision:
        with self._span("asap.policy.eval", {"asap.action": p.action, "asap.target": p.target}) as sp:
            d = self.plane.submit(run, p)
            sp.set_attribute("asap.verdict", d.verdict)
            sp.set_attribute("asap.tier", d.tier)
        run.decision = d
        m.ACTIONS.labels(p.action, str(d.tier), d.verdict).inc()
        if d.verdict == "deny":
            for r in d.reasons:
                m.DENIALS.labels(r[:60]).inc()
        self._log("policy", "decision", {"verdict": d.verdict, "tier": d.tier, "reasons": d.reasons,
                                         "blast_radius": d.blast_radius, "engine": d.policy_engine,
                                         "dry_run": vars(d.dry_run) if d.dry_run else None,
                                         "input": d.policy_input})
        self.ui.decision(run, d)
        return d

    def _policy_to_execution(self, run: RunState, p: Proposal) -> None:
        """POLICY_CHECK -> [AWAIT_APPROVAL] -> EXECUTE -> VERIFY, with at most one re-evaluation after drift.

        Ordering matters: the target lease is taken BEFORE re-validation (which re-reads the remediation
        budget) and held until verification ends, so no second actor can pass the budget check or act on the
        target in between."""
        lease = f"target:{p.target}"
        while True:
            d = self._decide(run, p)
            if d.verdict == "deny":
                return self._to(S.REPORT_ONLY, "policy denied: " + "; ".join(d.reasons))
            if d.verdict == "require_approval":
                self._to(S.AWAIT_APPROVAL, "; ".join(d.reasons))
                res = self._request_approval(run, p, d)
                if not res["approved"]:
                    return self._to(S.REPORT_ONLY, f"not approved: {res['reason']}")
                self._to(S.EXECUTE, f"approved by {', '.join(res['approvers'])}")
            else:
                self._to(S.EXECUTE, "tier 1: within auto-remediation bounds")

            # ---------------------------------------------------------- EXECUTE (fail closed)
            firing = self._firing(run)
            if firing is None:
                return self._to(S.REPORT_ONLY, "cannot confirm alert state before execution; failing closed")
            if not firing:
                return self._to(S.REPORT_ONLY, "alerts self-resolved before execution; nothing to do")
            if not self.store.acquire_lease(lease, run.run_id, self._clock()):
                return self._to(S.REPORT_ONLY, f"another actor holds the lease on {p.target}")
            try:
                approved_hash = (run.approval or {}).get("state_hash") if d.verdict == "require_approval" else None
                ok, why, fresh = self.plane.revalidate(run, p, d, approved_hash)
                self._log("policy", "revalidation", {"ok": ok, "reason": why, "fresh_verdict": fresh.verdict,
                                                     "fresh_reasons": fresh.reasons})
                if not ok:
                    if fresh.verdict == "deny" or not self._reevaluation_allowed(run):
                        return self._to(S.REPORT_ONLY, f"re-validation failed: {why}")
                    self._to(S.POLICY_CHECK, why)
                    continue
                ex = self._execute(run, p, d)
                if ex is None:
                    if run.state in S.TERMINAL:
                        return None
                    continue  # precondition drift: _execute moved the run back to POLICY_CHECK
                run.execution = ex
                self._log("executor", "applied", ex)
                self.ui.execution(run, ex)
                self._to(S.VERIFY, "applied; waiting for SLI recovery")
                return self._verify(run, p, ex)
            finally:
                self.store.release_lease(lease, run.run_id)

    def _request_approval(self, run: RunState, p: Proposal, d: Decision) -> dict:
        owners = d.policy_input.get("target", {}).get("owners") or []
        packet = self._approval_packet(run, p, d)
        with self._span("asap.approval", {"asap.required": d.required_approvals}) as sp:
            res = self.approval.request(packet, owners, d.required_approvals)
            sp.set_attribute("asap.approved", res["approved"])
        run.approval = res
        self._log("human", "approval", res)
        self.ui.approval(run, res)
        return res

    def _execute(self, run: RunState, p: Proposal, d: Decision) -> dict | None:
        """Apply under the lease. Returns the execution record, or None after a transition (drift or failure)."""
        self.store.record_action(p.proposal_id, run.run_id, p.target, p.action, self._clock())
        precondition = d.dry_run.precondition_resource_version if d.dry_run else None
        with self._span("asap.execute", {"asap.action": p.action, "asap.target": p.target}):
            try:
                return self.executor.execute(p, precondition)
            except PreconditionFailed as e:
                self.store.set_outcome(p.proposal_id, "aborted_precondition")
                log.warning("precondition failed for %s: %s", p.proposal_id, e)
                if self._reevaluation_allowed(run):
                    self._to(S.POLICY_CHECK, str(e))
                else:
                    self._to(S.REPORT_ONLY, str(e))
                return None
            except Exception as e:  # noqa: BLE001 - crash mid-apply: reconcile from observed state
                log.exception("executor failed mid-apply for %s", p.proposal_id)
                rec = self.executor.recover(p)
                self._log("executor", "crash_recovered", {"error": str(e), "reconciled": rec})
                if not rec or not rec.get("treated_as_applied"):
                    self.store.set_outcome(p.proposal_id, "failed")
                    self._to(S.REPORT_ONLY, f"execution failed ({e}); escalated")
                    return None
                return {"applied": True, "reconciled_after_crash": True, "previous": rec.get("previous", {})}

    def _reevaluation_allowed(self, run: RunState) -> bool:
        return sum(1 for src, dst, _ in run.transitions if src == S.EXECUTE and dst == S.POLICY_CHECK) \
            < self.limits.max_reevaluations

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
    def _ask(self, phase: str, tools: list[str]) -> LLMResponse:
        remaining = self._deadline - time.time()
        if remaining <= 0:
            raise DeadlineExceeded()
        run = self._run
        specs = tool_specs(tools, self._services)
        compaction = self._ctx.maybe_compact(prompts.SYSTEM, specs, self._messages, run)
        if compaction:
            run.compactions += 1
            self._log("system", "context_compacted", compaction)
        context_tokens = estimate_tokens(prompts.SYSTEM, specs, self._messages)
        run.peak_context_tokens = max(run.peak_context_tokens, context_tokens)
        self._ctx.check_budget(run, context_tokens)  # raises TokenBudgetExceeded before spending
        with self._span("gen_ai.chat", {"gen_ai.system": self.llm.name, "gen_ai.request.model": self.llm.model,
                                        "asap.phase": phase, "asap.context_tokens_est": context_tokens}) as sp:
            resp = self.llm.next(prompts.SYSTEM, self._messages, specs, run, phase,
                                 min(self.limits.llm_call_timeout_s, remaining))
            sp.set_attribute("gen_ai.usage.input_tokens", resp.tokens_in)
            sp.set_attribute("gen_ai.usage.output_tokens", resp.tokens_out)
            sp.set_attribute("gen_ai.usage.cache_read_input_tokens", resp.cache_read_tokens)
            sp.set_attribute("gen_ai.response.tool", resp.tool_name)
        if not isinstance(resp.tool_args, dict):  # e.g. an OpenAI-compatible model returned a JSON list
            resp.tool_args = {"_non_object_arguments": resp.tool_args}
        # Under forced tool choice Claude emits no free text, so the schema's `reasoning` field carries it;
        # models that only allow tool_choice=auto may also send text, which is kept as the thought.
        reasoning = resp.thought or str(resp.tool_args.get("reasoning", ""))
        run.tokens_in += resp.tokens_in
        run.tokens_out += resp.tokens_out
        run.tokens_cache_read += resp.cache_read_tokens
        processed = resp.tokens_in + resp.cache_read_tokens + resp.cache_write_tokens
        run.budget_tokens_in += processed if processed else context_tokens  # no provider usage: use the estimate
        m.TOKENS.labels(resp.model or self.llm.model, "in").inc(resp.tokens_in)
        m.TOKENS.labels(resp.model or self.llm.model, "out").inc(resp.tokens_out)
        self._messages.append({"role": "assistant", "text": resp.thought,
                               "tool_call": {"id": resp.tool_id, "name": resp.tool_name, "args": resp.tool_args}})
        self._log("agent", "llm_turn", {"phase": phase, "tool": resp.tool_name, "args": resp.tool_args,
                                        "reasoning": reasoning, "context_tokens_est": context_tokens,
                                        "tokens_in": resp.tokens_in, "cache_read_tokens": resp.cache_read_tokens,
                                        "tokens_out": resp.tokens_out, "latency_ms": round(resp.latency_ms, 1)})
        self.ui.llm(run, phase, resp, reasoning)
        return resp

    def _record_plan(self, resp: LLMResponse) -> bool:
        """Validate and store a plan. On failure the error is fed back to the model and the old plan kept."""
        try:
            plan = self.gateway.validate("submit_plan", resp.tool_args).model_dump(exclude={"reasoning"})  # type: ignore[attr-defined]
        except ToolRejected as e:
            self._tool_error(resp, f"plan rejected: {e}")
            return False
        self._run.plan = plan
        self._log("agent", "plan", plan)
        left = self.limits.max_steps - self._run.steps
        self._tool_ok(resp, {"status": "plan recorded"}, prompts.PHASE_INVESTIGATE.format(steps=left))
        return True

    def _read_tool(self, resp: LLMResponse) -> None:
        with self._span(f"asap.tool.{resp.tool_name}", {"asap.args": json.dumps(resp.tool_args, default=str)}) as sp:
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

    def _tool_ok(self, resp: LLMResponse, content: dict, suffix: str = "") -> None:
        text = json.dumps(content, default=str)
        self._messages.append({"role": "tool", "tool_call_id": resp.tool_id, "name": resp.tool_name,
                               "content": text + ("\n\n" + suffix if suffix else ""), "is_error": False})

    def _tool_error(self, resp: LLMResponse, err: str) -> None:
        self._log("system", "tool_rejected", {"tool": resp.tool_name, "args": resp.tool_args, "error": err})
        self.ui.rejected(self._run, resp.tool_name, err)
        self._messages.append({"role": "tool", "tool_call_id": resp.tool_id, "name": resp.tool_name,
                               "content": json.dumps({"error": err}), "is_error": True})

    # ================================================================== helpers
    def _firing(self, run: RunState) -> bool | None:
        """True/False if the incident's alerts are (not) firing; None if alert state can't be read."""
        try:
            firing = {a["labels"]["service"] for a in self.sim.alerts()}
        except Exception as e:  # noqa: BLE001
            log.warning("cannot read alert state for %s: %s", run.incident_id, e)
            return None
        return bool(firing & set(run.services))

    def _catalog(self) -> list[str]:
        try:
            return sorted(self.sim.catalog())
        except Exception as e:  # noqa: BLE001 - enums are a convenience; the gateway still validates
            log.warning("service catalog unavailable, tool schemas will not carry enums: %s", e)
            return []

    def _runbook_hints(self, run: RunState) -> list[str]:
        try:
            return [f"RB-017 bad-deploy signature: {run.root_service} revision {r['revision']} deployed "
                    f"{r['age_minutes']} min ago" for r in self.sim.history(run.root_service)
                    if r["current"] and r["age_minutes"] <= 15]
        except Exception as e:  # noqa: BLE001 - hints are optional
            log.warning("runbook hints unavailable for %s: %s", run.root_service, e)
            return []

    def _approval_packet(self, run: RunState, p: Proposal, d: Decision) -> dict:
        rv = d.dry_run.precondition_resource_version if d.dry_run else None
        cited = [run.evidence[e] for e in p.evidence_ids if e in run.evidence]
        return {"incident": run.incident_id, "run": run.run_id, "diagnosis": run.diagnosis,
                "proposal": {"action": p.action, "target": p.target, "params": p.params, "rationale": p.rationale},
                "dry_run_diff": d.dry_run.diff if d.dry_run else {}, "policy_reasons": d.reasons,
                "blast_radius": d.blast_radius, "required_approvals": d.required_approvals,
                "evidence": [{"id": e.evidence_id, "tool": e.tool, "args": e.args} for e in cited],
                "plan": run.plan, "state_hash": state_hash(p.target, rv, p.params)}

    def _clock(self) -> float:
        """Authoritative time for leases and budgets. Strict: a failure propagates (and fails the run closed)
        rather than silently mixing in the local wall clock."""
        return self.sim.now()

    def _sim_now_or_wall(self) -> float:
        try:
            return self.sim.now()
        except Exception:  # noqa: BLE001 - only used for record timestamps, never for decisions
            return time.time()

    def _log(self, actor: str, event: str, payload: dict) -> None:
        try:
            sim_time: float | None = self.sim.now()
        except Exception:  # noqa: BLE001
            sim_time = None
        try:
            self._audit.append(actor, event, payload, sim_time)
        except OSError:
            log.exception("audit append failed for %s/%s", actor, event)

    def _span(self, name: str, attrs: dict | None = None):  # type: ignore[no-untyped-def]
        return self.tracer.start_as_current_span(name, attributes=attrs or {})

    def _finish(self, run: RunState, incident: Incident, span) -> None:  # type: ignore[no-untyped-def]
        if run.state not in S.TERMINAL:  # defensive: every path above should already be terminal
            run.transitions.append((run.state, S.REPORT_ONLY, "run ended without a terminal state"))
            run.state, run.outcome = S.REPORT_ONLY, S.REPORT_ONLY
            run.outcome_reason = run.outcome_reason or "run ended without a terminal state"
        try:
            self.store.release_lease(f"incident:{run.incident_id}", run.run_id)
            self.store.save_run(run.run_id, run.incident_id, run.state, self._sim_now_or_wall(), run.to_dict())
        except Exception:  # noqa: BLE001
            log.exception("failed to persist final state for %s", run.run_id)
        span.set_attribute("asap.outcome", run.outcome or "")
        span.set_attribute("gen_ai.usage.input_tokens", run.tokens_in)
        span.set_attribute("gen_ai.usage.output_tokens", run.tokens_out)
        self._log("system", "run_finished", {"outcome": run.outcome, "reason": run.outcome_reason,
                                             "steps": run.steps, "tokens_in": run.tokens_in,
                                             "tokens_out": run.tokens_out, "tokens_cache_read": run.tokens_cache_read,
                                             "budget_tokens_in": run.budget_tokens_in,
                                             "peak_context_tokens": run.peak_context_tokens,
                                             "compactions": run.compactions,
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
