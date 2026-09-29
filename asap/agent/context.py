"""Context management for the agent loop: token estimation, compaction, and the per-run token budget.

Why: the loop re-sends the whole conversation on every turn, so billed input tokens grow roughly
quadratically with the number of steps. Two controls bound it:

1. Prompt caching of the whole conversation prefix (the Anthropic adapter marks the last block as a cache
   breakpoint). This is the COST lever: each turn re-reads the previous turn from cache at ~0.1x.
2. Threshold compaction, a SAFETY VALVE for context size, not a cost optimisation. When the context passes
   `compact_at_tokens`, every tool result except the most recent `keep_recent` is replaced, in place, by a
   one-line digest that keeps its evidence_id. Measured with `asap tokens`: compacting early (at 6k)
   *raises* cached cost, because each compaction rewrites history and invalidates the cached prefix. So the
   threshold sits well above normal runs (peak ~6-10k) and only fires for runaway contexts. The full result
   stays in run state; the model can still cite the evidence ID, or re-query for detail.
3. A per-run input-token budget, checked BEFORE each call against the projected spend. Exceeding it ends
   the run as REPORT_ONLY with the evidence gathered so far: a cap, never a retry.
"""

from __future__ import annotations

import json

from ..models import RunState

# Calibrated against the first live Claude run: prompts are mostly JSON (tool schemas, tool results), which
# tokenises more densely than prose. chars/4 under-counted real prompt size by ~1.6x; chars/2.5 matches it.
# Budget accounting uses the provider's exact usage whenever it is reported; this only drives projections.
CHARS_PER_TOKEN = 2.5


class TokenBudgetExceeded(Exception):
    pass


def estimate_tokens(*parts: object) -> int:
    n = 0
    for p in parts:
        n += len(p) if isinstance(p, str) else len(json.dumps(p, default=str))
    return int(n / CHARS_PER_TOKEN) + 1


def digest_result(tool: str, r: dict) -> str:
    """A one-line summary of a tool result, keeping the facts a diagnosis would cite."""
    try:
        if tool == "query_metrics":
            cp = f", changed {r.get('minutes_since_change')} min ago" if r.get("change_point") else ", no change point"
            return f"{r['metric']}({r['service']}): baseline {r['baseline']} -> current {r['current']}, peak {r['peak']}{cp}"
        if tool == "search_logs":
            cl = r.get("clusters", [])
            top = "; ".join(f"{c['count']}x {c['level']} {c['template'][:90]}" for c in cl[:3])
            return f"logs({r.get('service')}): {len(cl)} clusters: {top}"
        if tool == "get_traces":
            d = (r.get("downstream") or [{}])[0]
            if not d:
                return f"traces({r.get('service')}): root p99 {r.get('root_p99_ms')} ms; no downstream calls"
            return (f"traces({r.get('service')}): root p99 {r.get('root_p99_ms')} ms; top downstream "
                    f"{d.get('peer.service')} {d.get('share_of_root_p99', 0):.0%} of p99, db.system={d.get('db.system')}")
        if tool == "get_deployment_history":
            revs = "; ".join(f"rev {x['revision']} {x['version']} ({x['age_minutes']:.0f}m ago"
                             f"{', current' if x['current'] else ''})" for x in r.get("revisions", [])[:4])
            return f"deploys({r.get('service')}): {revs}"
        if tool == "get_resource_state":
            return (f"state({r['name']}): {r['kind']} tier-{r['tier']} replicas={r['replicas']} hpa={r['hpa']} "
                    f"pdb={r['pdb']} owners={r.get('owners')}")
        if tool == "get_service_dependencies":
            return f"deps({r['service']}): downstream={r['downstream']} upstream={r['upstream']}"
    except (KeyError, TypeError, ValueError):
        pass
    s = json.dumps(r, default=str)
    return s if len(s) <= 200 else s[:199] + "…"


class ContextManager:
    def __init__(self, compact_at_tokens: int, keep_recent: int, max_run_input_tokens: int) -> None:
        self.compact_at_tokens = compact_at_tokens
        self.keep_recent = keep_recent
        self.max_run_input_tokens = max_run_input_tokens
        self.compactions = 0

    def maybe_compact(self, system: str, tools: list[dict], messages: list[dict], run: RunState) -> dict | None:
        """Compact older tool results in place if the context is over threshold. Returns stats or None."""
        before = estimate_tokens(system, tools, messages)
        if before <= self.compact_at_tokens:
            return None
        tool_msgs = [m for m in messages if m["role"] == "tool" and not m.get("compacted")]
        candidates = tool_msgs[:-self.keep_recent] if self.keep_recent else tool_msgs
        changed = 0
        for m in candidates:
            try:
                payload = json.loads(m["content"].split("\n\n", 1)[0])
            except (json.JSONDecodeError, AttributeError):
                continue
            eid = payload.get("evidence_id") if isinstance(payload, dict) else None
            if not eid or eid not in run.evidence:
                continue  # errors, acks and plan receipts are small: keep them verbatim
            ev = run.evidence[eid]
            m["content"] = json.dumps({"evidence_id": eid, "compacted": True, "digest": digest_result(ev.tool, ev.result),
                                       "note": "older result summarised to save context; cite the evidence_id, "
                                               "or re-query for full detail"})
            m["compacted"] = True
            changed += 1
        if not changed:
            return None
        self.compactions += 1
        return {"before_tokens": before, "after_tokens": estimate_tokens(system, tools, messages),
                "results_compacted": changed, "kept_recent": self.keep_recent}

    def check_budget(self, run: RunState, projected_call_tokens: int) -> None:
        """Raise before a call that would push this run over its input-token budget."""
        spent = run.budget_tokens_in
        if spent + projected_call_tokens > self.max_run_input_tokens:
            raise TokenBudgetExceeded(f"per-run input-token budget {self.max_run_input_tokens:,} would be exceeded "
                                      f"(spent {spent:,}, next call ~{projected_call_tokens:,})")
