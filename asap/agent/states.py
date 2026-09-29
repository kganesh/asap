"""The run state machine. Code, not the LLM, decides which state comes next."""

from __future__ import annotations

from ..config import Limits  # noqa: F401 - run caps live with the other settings; re-exported for callers

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

