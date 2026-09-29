"""The run state machine. Code, not the LLM, decides which state comes next."""

from __future__ import annotations

TRIAGE = "TRIAGE"
PLAN = "PLAN"
INVESTIGATE = "INVESTIGATE"
DIAGNOSE = "DIAGNOSE"
PROPOSE = "PROPOSE"
POLICY_CHECK = "POLICY_CHECK"
AWAIT_APPROVAL = "AWAIT_APPROVAL"
EXECUTE = "EXECUTE"
VERIFY = "VERIFY"
RESOLVED = "RESOLVED"
REVERTED = "REVERTED_ESCALATED"
REPORT_ONLY = "REPORT_ONLY"

TERMINAL = {RESOLVED, REVERTED, REPORT_ONLY}
LLM_STATES = {PLAN, INVESTIGATE, PROPOSE}

ALLOWED: dict[str, set[str]] = {
    TRIAGE: {PLAN, REPORT_ONLY},
    PLAN: {INVESTIGATE, REPORT_ONLY},
    INVESTIGATE: {PLAN, DIAGNOSE, REPORT_ONLY},  # PLAN = replan edge (max 2)
    DIAGNOSE: {PROPOSE, REPORT_ONLY},
    PROPOSE: {POLICY_CHECK, REPORT_ONLY},
    POLICY_CHECK: {EXECUTE, AWAIT_APPROVAL, REPORT_ONLY},
    AWAIT_APPROVAL: {EXECUTE, REPORT_ONLY},
    EXECUTE: {VERIFY, POLICY_CHECK, REPORT_ONLY},  # POLICY_CHECK = re-evaluation after drift
    VERIFY: {RESOLVED, REVERTED, REPORT_ONLY},  # REPORT_ONLY: verification itself failed
}


class IllegalTransition(Exception):
    pass


def check(src: str, dst: str) -> None:
    if dst not in ALLOWED.get(src, set()):
        raise IllegalTransition(f"{src} -> {dst} is not a legal transition")


class Limits:
    def __init__(self, max_steps: int = 15, max_replans: int = 2, deadline_s: float = 180.0,
                 llm_call_timeout_s: float = 60.0, max_reevaluations: int = 1, max_invalid_diagnoses: int = 2,
                 max_run_input_tokens: int = 200_000, compact_at_tokens: int = 16_000, keep_recent_results: int = 3) -> None:
        self.max_steps = max_steps
        self.max_replans = max_replans
        self.deadline_s = deadline_s
        self.llm_call_timeout_s = llm_call_timeout_s
        self.max_reevaluations = max_reevaluations
        self.max_invalid_diagnoses = max_invalid_diagnoses
        self.max_run_input_tokens = max_run_input_tokens  # billed input across all calls in one run
        self.compact_at_tokens = compact_at_tokens  # context size that triggers compaction of older results
        self.keep_recent_results = keep_recent_results  # tool results always kept verbatim
