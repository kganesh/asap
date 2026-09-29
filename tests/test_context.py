"""Context growth controls: compaction (safety valve), per-run token budget, prefix caching, accounting."""

from __future__ import annotations

import json

from asap.agent.adversarial import PLAN, ScriptedLLM, ev
from asap.agent.context import digest_result
from asap.agent.llm import AnthropicLLM, LLMResponse
from asap.agent.states import Limits
from asap.agent.tokenreport import Measured
from asap.control.approval import ApprovalGate
from asap.harness import run_scenario

READS = [("search_logs", {"service": s, "window_minutes": 60, "limit": 20})
         for s in ["checkout", "frontend", "payments", "inventory", "cart", "checkout"]]


def test_compaction_digests_old_results_but_keeps_evidence_citable(env):
    diag = ("submit_diagnosis", lambda r: {"root_cause": "x", "root_service": "checkout", "category": "unknown",
                                           "confidence": 0.3, "evidence_ids": ev(r)[:1], "recommended_action": "none"})
    llm = ScriptedLLM([PLAN, *READS, diag, ("no_action", {"reason": "done"})])
    env.load("bad_deploy")
    orch = env.orchestrator(llm, ApprovalGate("auto"), limits=Limits(compact_at_tokens=3_000, keep_recent_results=2))
    run = orch.run(env.incidents()[0])
    tool_msgs = [m for m in orch._messages if m["role"] == "tool" and "evidence_id" in m["content"]]
    compacted = [m for m in tool_msgs if m.get("compacted")]
    assert run.compactions >= 1 and compacted
    assert all(json.loads(m["content"])["evidence_id"] in run.evidence for m in compacted)
    assert not tool_msgs[-1].get("compacted"), "the most recent results stay verbatim"
    assert run.diagnosis is not None, "a diagnosis citing a compacted (oldest) evidence ID is still accepted"


def test_token_budget_ends_run_before_overspending(env):
    llm = ScriptedLLM([PLAN, *READS * 3], loop_last=True)
    run, _ = run_scenario(env, "bad_deploy", llm, ApprovalGate("auto"), limits=Limits(max_run_input_tokens=15_000))
    assert run.outcome == "REPORT_ONLY" and "token budget" in run.outcome_reason
    assert run.budget_tokens_in <= 15_000


def test_budget_uses_provider_usage_when_reported(env):
    class Metered(ScriptedLLM):
        def next(self, *a, **k) -> LLMResponse:  # type: ignore[no-untyped-def]
            r = super().next(*a, **k)
            r.tokens_in, r.cache_read_tokens, r.cache_write_tokens = 100, 4_000, 50
            return r

    run, _ = run_scenario(env, "bad_deploy", Metered([PLAN, ("no_action", {"reason": "x"})]), ApprovalGate("auto"))
    assert run.budget_tokens_in > 0 and run.budget_tokens_in % 4_150 == 0
    assert run.tokens_cache_read == 4_000 * (run.budget_tokens_in // 4_150)


def test_anthropic_caches_the_conversation_tail():
    msgs = [{"role": "user", "text": "incident"},
            {"role": "assistant", "text": "", "tool_call": {"id": "t1", "name": "get_traces", "args": {}}},
            {"role": "tool", "tool_call_id": "t1", "name": "get_traces", "content": "{}", "is_error": False}]
    out = AnthropicLLM.to_messages(msgs, cache_tail=True)
    assert out[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in AnthropicLLM.to_messages(msgs)[-1]["content"][-1]


def test_cost_model_prefers_caching_over_early_compaction():
    grow = Measured(prompts=[4000, 5000, 6000, 7000], prefixes=[3000] * 4)
    compact = Measured(prompts=[4000, 5000, 4200, 5200], prefixes=[3000] * 4, compacted_at={2})
    assert grow.cost_equivalent() < compact.cost_equivalent() * 1.2  # compaction saves little once cached


def test_digest_keeps_the_facts_a_diagnosis_cites():
    d = digest_result("query_metrics", {"metric": "error_ratio", "service": "checkout", "baseline": 0.005,
                                        "current": 0.18, "peak": 0.19, "change_point": "t", "minutes_since_change": 10.5})
    assert "0.005 -> current 0.18" in d and "10.5 min ago" in d
