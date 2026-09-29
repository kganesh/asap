"""Regression tests for the code-review findings (each test names the failure it pins down)."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import httpx

from asap.agent.adversarial import PLAN, ScriptedLLM
from asap.agent.llm import AnthropicLLM
from asap.agent.scripted import DeterministicReasoner
from asap.control.approval import ApprovalGate
from asap.harness import run_scenario
from asap.models import Proposal
from asap.tools.schemas import tool_specs

from .conftest import needs_policy


def test_telemetry_outage_in_control_plane_fails_closed_without_crashing(env):
    """Before: httpx.ConnectError escaped Orchestrator.run() and crashed the CLI."""
    env.load("cpu_throttle")
    orch = env.orchestrator(DeterministicReasoner(), ApprovalGate("auto"))

    def down() -> float:
        raise httpx.ConnectError("telemetry down")

    orch.plane.sim.now = down  # the control plane loses its read path mid-run
    run = orch.run(env.incidents()[0])
    assert run.outcome == "REPORT_ONLY" and "failing closed" in run.outcome_reason
    assert run.execution is None


def test_unexpected_exception_never_escapes_run(env):
    env.load("bad_deploy")
    orch = env.orchestrator(DeterministicReasoner(), ApprovalGate("auto"))
    orch.plane.submit = lambda *a, **k: (_ for _ in ()).throw(ValueError("bug in a plugin"))
    run = orch.run(env.incidents()[0])
    assert run.outcome == "REPORT_ONLY" and "ValueError" in run.outcome_reason


@needs_policy
def test_target_lease_is_held_through_revalidation_and_verification(env):
    """Before: the budget was re-read before the lease was taken, and the lease was dropped before verify."""
    env.load("cpu_throttle")
    orch = env.orchestrator(DeterministicReasoner(), ApprovalGate("auto"))
    seen = {}
    real_revalidate, real_verify = orch.plane.revalidate, orch.executor.verify

    def revalidate(*a, **k):
        seen["reval"] = env.store.acquire_lease("target:payments", "run-intruder", env.server.world.now)
        return real_revalidate(*a, **k)

    def verify(*a, **k):
        seen["verify"] = env.store.acquire_lease("target:payments", "run-intruder", env.server.world.now)
        return real_verify(*a, **k)

    orch.plane.revalidate, orch.executor.verify = revalidate, verify
    run = orch.run(env.incidents()[0])
    assert run.outcome == "RESOLVED"
    assert seen == {"reval": False, "verify": False}, "a second actor must not get the lease mid-flight"


def test_competing_actor_holding_the_target_blocks_execution(env):
    env.load("cpu_throttle")
    env.store.acquire_lease("target:payments", "human-kubectl-session", env.server.world.now)
    run, _ = run_scenario(env, "cpu_throttle", DeterministicReasoner(), ApprovalGate("auto"))
    assert run.outcome == "REPORT_ONLY" and "lease" in run.outcome_reason and run.execution is None


@needs_policy
def test_unknown_alert_state_blocks_execution(env):
    """Before: _still_firing returned True on error, so the pre-execution gate failed OPEN."""
    env.load("cpu_throttle")
    orch = env.orchestrator(DeterministicReasoner(), ApprovalGate("auto"))
    real_alerts, calls = orch.sim.alerts, {"n": 0}

    def flaky_alerts():
        calls["n"] += 1
        if calls["n"] >= 3:  # triage + pre-propose succeed; the pre-execution read fails
            raise httpx.ConnectError("alertmanager down")
        return real_alerts()

    orch.sim.alerts = flaky_alerts
    run = orch.run(env.incidents()[0])
    assert run.outcome == "REPORT_ONLY" and "cannot confirm alert state" in run.outcome_reason
    assert run.execution is None


def test_approval_ttl_is_enforced():
    gate = ApprovalGate("prompt", ask=lambda prompt: time.sleep(5) or "y", ttl_seconds=0.2)
    t0 = time.time()
    res = gate.request({"state_hash": "x"}, ["alice"], 1)
    assert not res["approved"] and "no response" in res["reason"] and time.time() - t0 < 2


def test_eof_at_the_approval_prompt_is_a_rejection():
    def eof(prompt: str) -> str:
        raise EOFError

    res = ApprovalGate("prompt", ask=eof, ttl_seconds=2).request({"state_hash": "x"}, ["alice"], 1)
    assert not res["approved"]


def test_invalid_plan_is_fed_back_and_retried(env):
    """Before: an invalid plan was stored as '(invalid plan)' and the model was told 'plan recorded'."""
    llm = ScriptedLLM([("submit_plan", {"hypotheses": [], "steps": []}), PLAN,
                       ("no_action", {"reason": "done"})], loop_last=True)
    env.load("db_red_herring")
    orch = env.orchestrator(llm, ApprovalGate("auto"))
    run = orch.run(env.incidents()[0])
    errors = [m for m in orch._messages if m["role"] == "tool" and m["is_error"]]
    assert errors and "plan rejected" in errors[0]["content"]
    assert run.plan and run.plan["hypotheses"] == ["whatever"]


def test_reasoning_is_recorded_for_every_llm_turn(env):
    run, _ = run_scenario(env, "db_red_herring", DeterministicReasoner(), ApprovalGate("auto"))
    turns = [json.loads(line) for line in (env.runs_dir / run.run_id / "audit.jsonl").read_text().splitlines()]
    llm_turns = [t for t in turns if t["event"] == "llm_turn"]
    assert llm_turns and all(t["payload"]["reasoning"] for t in llm_turns)


def test_tool_specs_require_reasoning_and_enumerate_services():
    spec = tool_specs(["propose_rollback"], services=["checkout", "payments"])[0]
    schema = spec["input_schema"]
    assert list(schema["properties"])[0] == "reasoning" and "reasoning" in schema["required"]
    assert schema["properties"]["deployment"]["enum"] == ["checkout", "payments"]
    assert "Revision NUMBER" in schema["properties"]["to_revision"]["description"]


@needs_policy
def test_crash_after_restart_is_reconciled_from_observed_state(env):
    env.load("bad_deploy")
    orch = env.orchestrator(DeterministicReasoner(), ApprovalGate("auto"))
    p = Proposal("prop-r", "run-x", "inc-x", "restart", "checkout", {}, [], "", 0.9)
    orch.executor.crash_after_apply = True
    try:
        orch.executor.execute(p, None)
    except RuntimeError:
        pass
    rec = orch.executor.recover(p)
    assert rec["observed_applied"] is True and rec["treated_as_applied"]
    assert orch.executor.execute(p, None)["idempotent_replay"]  # never applied twice


def test_leases_are_atomic_across_connections(tmp_path):
    from asap.control.store import StateStore

    a, b = StateStore(tmp_path / "s.db"), StateStore(tmp_path / "s.db")
    assert a.acquire_lease("t", "A", 100)
    assert not b.acquire_lease("t", "B", 100)
    assert b.acquire_lease("t", "B", 100 + 901)  # expired


def test_anthropic_truncated_response_is_retried_once():
    import anthropic

    calls = []

    def create(**kw):
        calls.append(kw["max_tokens"])
        if len(calls) == 1:
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="long...")], stop_reason="max_tokens",
                                   usage=SimpleNamespace(input_tokens=1, output_tokens=2048), model=kw["model"])
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", id="t", name="submit_plan",
                                                        input={"reasoning": "r", "hypotheses": ["h"], "steps": ["s"]})],
                               stop_reason="tool_use", usage=SimpleNamespace(input_tokens=1, output_tokens=5),
                               model=kw["model"])

    llm = AnthropicLLM.__new__(AnthropicLLM)
    llm._anthropic, llm.model, llm.fallback = anthropic, "claude-sonnet-5-5", None
    llm._tool_choice = {}
    llm.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    r = llm.next("sys", [{"role": "user", "text": "x"}], tool_specs(["submit_plan"]), None, "PLAN", 5)
    assert r.tool_name == "submit_plan" and calls == [2048, 4096]


def test_models_that_reject_forced_tool_choice_fall_back_to_auto_and_nudge():
    """Found in the first live run: Claude Sonnet 5.5 returns 400 for tool_choice any/tool."""
    import anthropic

    calls = []

    def create(**kw):
        calls.append((kw["tool_choice"]["type"], len(kw["messages"])))
        if kw["tool_choice"]["type"] == "any":
            raise anthropic.BadRequestError(
                "tool_choice: type \"tool\" and \"any\" are not supported for this model.",
                response=httpx.Response(400, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")),
                body=None)
        if len(calls) == 2:  # auto mode: the model answers in text first
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="I think it's the deploy.")],
                                   stop_reason="end_turn", usage=SimpleNamespace(input_tokens=10, output_tokens=5),
                                   model=kw["model"])
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="Planning."),
                                        SimpleNamespace(type="tool_use", id="t", name="submit_plan",
                                                        input={"reasoning": "r", "hypotheses": ["h"], "steps": ["s"]})],
                               stop_reason="tool_use", usage=SimpleNamespace(input_tokens=12, output_tokens=7),
                               model=kw["model"])

    llm = AnthropicLLM.__new__(AnthropicLLM)
    llm._anthropic, llm.model, llm.fallback = anthropic, "claude-sonnet-5-5", None
    llm._tool_choice = {}  # unknown to the static list: learned from the 400
    llm.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    r = llm.next("sys", [{"role": "user", "text": "x"}], tool_specs(["submit_plan"]), None, "PLAN", 5)
    assert r.tool_name == "submit_plan" and r.thought == "Planning."
    assert [c[0] for c in calls] == ["any", "auto", "auto"]
    assert calls[2][1] == calls[1][1] + 2, "the nudge adds the text reply and a user reminder"
    assert r.tokens_in == 22, "usage is summed across the retries"
    llm.next("sys", [{"role": "user", "text": "x"}], tool_specs(["submit_plan"]), None, "PLAN", 5)
    assert calls[3][0] == "auto", "the fallback to auto is remembered per model"


def test_known_auto_only_models_start_on_auto(monkeypatch):
    from asap.agent.llm import initial_tool_choice

    assert initial_tool_choice("claude-sonnet-5-5") == "auto"
    assert initial_tool_choice("claude-opus-5-5") == "auto"
    assert initial_tool_choice("claude-haiku-4-5-20251001") == "any"
    monkeypatch.setenv("ASAP_TOOL_CHOICE", "any")
    assert initial_tool_choice("claude-sonnet-5-5") == "any"
