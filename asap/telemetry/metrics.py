"""Prometheus metrics about the agent itself (the SLIs behind ASAP's own SLOs)."""

from __future__ import annotations

from pathlib import Path

from prometheus_client import CollectorRegistry, Counter, Histogram, write_to_textfile

REGISTRY = CollectorRegistry()
RUNS = Counter("asap_runs_total", "Agent runs by outcome", ["outcome"], registry=REGISTRY)
ACTIONS = Counter("asap_actions_total", "Proposed actions by verdict", ["action", "tier", "verdict"], registry=REGISTRY)
DENIALS = Counter("asap_policy_denials_total", "Policy denials by reason", ["reason"], registry=REGISTRY)
TOKENS = Counter("asap_llm_tokens_total", "LLM tokens", ["model", "direction"], registry=REGISTRY)
REJECTED_TOOL_CALLS = Counter("asap_tool_calls_rejected_total", "Tool calls rejected by the gateway", ["reason"],
                              registry=REGISTRY)
REVERTS = Counter("asap_remediation_reverted_total", "Remediations reverted or escalated after failed verification",
                  ["action"], registry=REGISTRY)
TIME_TO_DIAGNOSIS = Histogram("asap_time_to_diagnosis_seconds", "Wall-clock time from run start to diagnosis",
                              buckets=(1, 5, 15, 30, 60, 120, 180, 300), registry=REGISTRY)


def dump(path: Path) -> None:
    write_to_textfile(str(path), REGISTRY)
