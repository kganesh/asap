"""Production edge cases from the design review."""

from __future__ import annotations

import httpx
import pytest

from asap.agent.llm import LLMUnavailable
from asap.agent.scripted import DeterministicReasoner
from asap.agent.states import IllegalTransition, Limits, check
from asap.audit.log import verify_chain
from asap.control.approval import ApprovalGate
from asap.harness import run_scenario
from asap.models import Proposal
from asap.sim.api import READER_TOKEN

from .conftest import needs_policy


@needs_policy
def test_drift_after_approval_voids_it_and_reevaluates(env):
    calls = {"n": 0}

    def approve_while_someone_else_changes_the_target(prompt: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:  # a human restarts checkout by hand while the approval is pending
            env.server.world.rollout_restart("checkout")
        return "y"

    gate = ApprovalGate("prompt", ask=approve_while_someone_else_changes_the_target)
    run, _ = run_scenario(env, "bad_deploy", DeterministicReasoner(), gate)
    hops = [(a, b) for a, b, _ in run.transitions]
    assert ("EXECUTE", "POLICY_CHECK") in hops, "drift must send the run back to the policy check"
    assert calls["n"] == 2, "a fresh approval is required after drift"
    assert run.outcome == "RESOLVED"


@needs_policy
def test_executor_crash_after_apply_is_reconciled_not_reapplied(env):
    orch_holder = {}

    class CrashingUI:
        def __getattr__(self, name):
            return lambda *a, **k: None

    env.load("cpu_throttle")
    orch = env.orchestrator(DeterministicReasoner(), ApprovalGate("auto"), CrashingUI())
    orch.executor.crash_after_apply = True
    orch_holder["o"] = orch
    run = orch.run(env.incidents()[0])
    hpa_events = [e for e in env.server.world.events if e["kind"] == "hpa_min_changed"]
    assert len(hpa_events) == 1, "the action must be applied exactly once"
    assert run.outcome == "RESOLVED"


@needs_policy
def test_executor_is_idempotent_per_proposal(env):
    env.load("cpu_throttle")
    orch = env.orchestrator(DeterministicReasoner(), ApprovalGate("auto"))
    p = Proposal("prop-fixed", "run-x", "inc-x", "scale", "payments", {"replicas": 8}, [], "", 0.9)
    first = orch.executor.execute(p, None)
    second = orch.executor.execute(p, None)
    assert not first["idempotent_replay"] and second["idempotent_replay"]
    assert len([e for e in env.server.world.events if e["kind"] == "hpa_min_changed"]) == 1


def test_alert_resolved_before_investigation(env):
    env.load("bad_deploy")
    incident = env.incidents()[0]
    env.server.world.gitops_revert("checkout", 2)  # a human already fixed it
    env.server.world.advance(10)
    run = env.orchestrator(DeterministicReasoner(), ApprovalGate("auto")).run(incident)
    assert run.outcome == "REPORT_ONLY" and "resolved" in run.outcome_reason


def test_duplicate_incident_delivery_is_ignored(env):
    env.load("bad_deploy")
    incident = env.incidents()[0]
    env.store.acquire_lease(f"incident:{incident.incident_id}", "run-other", env.server.world.now)
    run = env.orchestrator(DeterministicReasoner(), ApprovalGate("auto")).run(incident)
    assert run.outcome == "REPORT_ONLY" and "duplicate" in run.outcome_reason


@needs_policy
def test_state_store_outage_fails_closed(env):
    env.store.unavailable = True
    try:
        run, _ = run_scenario(env, "cpu_throttle", DeterministicReasoner(), ApprovalGate("auto"))
    except Exception:
        pytest.fail("store outage must not crash the run")
    finally:
        env.store.unavailable = False
    assert run.outcome == "REPORT_ONLY" and run.execution is None


def test_llm_outage_degrades_to_report_only(env):
    class DownLLM:
        name, model = "down", "none"

        def next(self, *a, **k):
            raise LLMUnavailable("provider returned 529 overloaded")

    run, _ = run_scenario(env, "bad_deploy", DownLLM(), ApprovalGate("auto"))
    assert run.outcome == "REPORT_ONLY" and "LLM unavailable" in run.outcome_reason


def test_deadline_ends_run_with_report(env):
    run, _ = run_scenario(env, "bad_deploy", DeterministicReasoner(), ApprovalGate("auto"),
                          limits=Limits(deadline_s=0))
    assert run.outcome == "REPORT_ONLY" and "deadline" in run.outcome_reason


def test_agent_credential_cannot_write(env):
    r = httpx.post(f"{env.server.url}/admin/gitops/revert", json={"deployment": "checkout", "to_revision": 1},
                   headers={"Authorization": f"Bearer {READER_TOKEN}"})
    assert r.status_code == 403


def test_illegal_transitions_are_rejected():
    check("POLICY_CHECK", "EXECUTE")
    with pytest.raises(IllegalTransition):
        check("PROPOSE", "EXECUTE")  # the agent can never skip the control plane
    with pytest.raises(IllegalTransition):
        check("INVESTIGATE", "EXECUTE")


def test_audit_tampering_is_detected(env):
    run, _ = run_scenario(env, "db_red_herring", DeterministicReasoner(), ApprovalGate("auto"))
    path = env.runs_dir / run.run_id / "audit.jsonl"
    assert verify_chain(path)[0]
    lines = path.read_text().splitlines()
    import json

    rec = json.loads(lines[3])
    rec["payload"] = {"tampered": True}  # rewrite history without fixing the hash
    lines[3] = json.dumps(rec)
    path.write_text("\n".join(lines) + "\n")
    ok, msg = verify_chain(path)
    assert not ok and "modified" in msg
