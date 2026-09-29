"""Shared data model for a run. Run state lives outside the prompt; prompts are rebuilt from it."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


@dataclass
class Evidence:
    evidence_id: str
    tool: str
    kind: str  # metric | logs | traces | deploy | state | deps
    args: dict
    result: dict
    at: float


@dataclass
class Proposal:
    proposal_id: str
    run_id: str
    incident_id: str
    action: str  # rollback | scale | restart | cache_flush
    target: str
    params: dict
    evidence_ids: list[str]
    rationale: str
    diagnosis_confidence: float


@dataclass
class DryRun:
    ok: bool
    diff: dict
    precondition_resource_version: str | None
    crosses_migration: bool = False
    errors: list[str] = field(default_factory=list)


@dataclass
class Decision:
    proposal_id: str
    verdict: str  # allow | require_approval | deny
    tier: int
    reasons: list[str]
    blast_radius: int
    dry_run: DryRun | None
    policy_input: dict
    policy_engine: str
    required_approvals: int = 0


@dataclass
class RunState:
    run_id: str
    incident_id: str
    root_service: str
    services: list[str]
    alerts: list[dict]
    llm_name: str
    domain: str = ""  # failure domain (cluster/cell): the unit for fleet-wide budgets
    state: str = "TRIAGE"
    plan: dict | None = None
    replans: int = 0
    evidence: dict[str, Evidence] = field(default_factory=dict)
    diagnosis: dict | None = None
    proposal: Proposal | None = None
    decision: Decision | None = None
    approval: dict | None = None
    execution: dict | None = None
    verification: dict | None = None
    outcome: str | None = None
    outcome_reason: str = ""
    steps: int = 0
    tool_calls: int = 0
    tokens_in: int = 0  # provider-reported uncached input tokens
    tokens_out: int = 0
    tokens_cache_read: int = 0
    budget_tokens_in: int = 0  # all input tokens processed (uncached + cache read/write, or estimate): budgeted
    peak_context_tokens: int = 0
    compactions: int = 0
    flagged_untrusted: list[str] = field(default_factory=list)
    transitions: list[tuple[str, str, str]] = field(default_factory=list)
    started_wall: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["evidence"] = {k: {**asdict(v), "result": "<omitted>"} for k, v in self.evidence.items()}
        return d
