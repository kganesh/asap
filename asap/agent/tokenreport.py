"""`asap tokens`: measure what each run would send to an LLM, turn by turn.

Wraps the deterministic reasoner (typical runs) and a scripted verbose investigator (worst case) and
records the full prompt each call would carry: system + tool schemas + conversation. Token counts are
estimates (chars / 4); a live run records the provider's exact usage in the audit log instead.

The cost-equivalent column models Anthropic prompt caching: a cached prefix is billed at 0.1x, newly
written context at 1.25x. Without compaction each turn reads the previous turn's prompt from cache; a
compaction rewrites history, so that turn re-writes everything after the static prefix.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..control.approval import ApprovalGate
from ..harness import Env, run_scenario
from .adversarial import PLAN, ScriptedLLM
from .context import CHARS_PER_TOKEN
from .scripted import DeterministicReasoner
from .states import Limits

CACHE_READ, CACHE_WRITE = 0.1, 1.25


@dataclass
class Measured:
    prompts: list[int] = field(default_factory=list)
    prefixes: list[int] = field(default_factory=list)  # system + tools (static per phase)
    compacted_at: set[int] = field(default_factory=set)
    seen_compacted: int = 0

    def record(self, system: str, tools: list[dict], messages: list[dict]) -> None:
        prefix = (len(system) + len(json.dumps(tools))) // CHARS_PER_TOKEN
        total = prefix + len(json.dumps(messages)) // CHARS_PER_TOKEN
        compacted = sum(1 for m in messages if m.get("compacted"))
        if compacted > self.seen_compacted:  # history was rewritten before this call
            self.compacted_at.add(len(self.prompts))
        self.seen_compacted = compacted
        self.prompts.append(total)
        self.prefixes.append(prefix)

    def cost_equivalent(self) -> int:
        cost, prev = 0.0, 0
        for i, (p, f) in enumerate(zip(self.prompts, self.prefixes, strict=True)):
            if i == 0 or self.prefixes[i - 1] != f:  # first call or tool set changed: prefix written fresh
                read, write = 0, p
            elif i in self.compacted_at:
                read, write = f, p - f
            else:
                read, write = prev, p - prev
            cost += CACHE_READ * read + CACHE_WRITE * max(write, 0)
            prev = p
        return int(cost)


def _measuring(base):  # type: ignore[no-untyped-def]
    class M(base):  # type: ignore[misc, valid-type]
        def __init__(self, *a, **k):  # type: ignore[no-untyped-def]
            super().__init__(*a, **k)
            self.m = Measured()

        def next(self, system, messages, tools, run, phase, timeout_s):  # type: ignore[no-untyped-def]
            self.m.record(system, tools, messages)
            return super().next(system, messages, tools, run, phase, timeout_s)
    return M


VERBOSE = [PLAN] + [
    ("search_logs", {"service": s, "window_minutes": 60, "limit": 20}) if i % 2 == 0
    else ("get_traces", {"service": s, "window_minutes": 60})
    for i, s in enumerate(["checkout", "frontend", "payments", "inventory", "cart"] * 3)][:14] + \
    [("no_action", {"reason": "verbose worst case"})]


def measure(runs_dir: Path) -> list[dict]:
    rows = []
    env = Env.create(runs_dir)
    try:
        for sc in ("bad_deploy", "cpu_throttle", "db_red_herring"):
            llm = _measuring(DeterministicReasoner)()
            run, _ = run_scenario(env, sc, llm, ApprovalGate("auto"))
            rows.append(_row(sc, llm.m, run))
        for label, lim in (("worst case (defaults)", Limits()),
                           ("worst case, aggressive compaction @6k", Limits(compact_at_tokens=6_000)),
                           ("worst case, budget 40k", Limits(max_run_input_tokens=40_000))):
            llm = _measuring(ScriptedLLM)(VERBOSE)
            run, _ = run_scenario(env, "bad_deploy", llm, ApprovalGate("auto"), limits=lim)
            rows.append(_row(label, llm.m, run))
    finally:
        env.close()
    return rows


def _row(label: str, m: Measured, run) -> dict:  # type: ignore[no-untyped-def]
    return {"run": label, "llm_calls": len(m.prompts), "peak_context": max(m.prompts, default=0),
            "total_input": sum(m.prompts), "cost_equivalent_cached": m.cost_equivalent(),
            "compactions": run.compactions, "outcome": run.outcome, "reason": run.outcome_reason}
