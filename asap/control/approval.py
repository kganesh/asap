"""Human-in-the-loop approval gate.

Production: a Slack interactive message (signed request) plus a PagerDuty incident note; approver
identity is checked against service ownership in the catalog; tier-0 targets need two approvers;
the approval is bound to a hash of the target's state and voided if that state changes.

PoC: the same rules, with a CLI prompt standing in for Slack.
"""

from __future__ import annotations

import hashlib
import sys
from typing import Callable

APPROVAL_TTL_MINUTES = 15


def state_hash(target: str, resource_version: str | None, params: dict) -> str:
    return hashlib.sha256(f"{target}|{resource_version}|{sorted(params.items())}".encode()).hexdigest()[:16]


class ApprovalGate:
    """mode: prompt | auto | deny | timeout"""

    def __init__(self, mode: str = "auto", render: Callable[[dict], None] | None = None,
                 ask: Callable[[str], str] | None = None) -> None:
        self.mode = mode
        self.render = render or (lambda packet: None)
        self.ask = ask or input

    def request(self, packet: dict, owners: list[str], required: int) -> dict:
        self.render(packet)
        base = {"channel": "cli (stand-in for Slack interactive message + PagerDuty note)",
                "ttl_minutes": APPROVAL_TTL_MINUTES, "required_approvals": required,
                "state_hash": packet["state_hash"]}
        if self.mode == "timeout":
            return {**base, "approved": False, "approvers": [],
                    "reason": f"no response within {APPROVAL_TTL_MINUTES} min; escalated (never auto-approved)"}
        if self.mode == "deny":
            return {**base, "approved": False, "approvers": [owners[0]], "reason": "rejected by approver"}
        approvers: list[str] = []
        candidates = list(owners)
        for i in range(required):
            if not candidates:
                return {**base, "approved": False, "approvers": approvers,
                        "reason": f"needs {required} distinct owners; service catalog lists {len(owners)}"}
            who = candidates.pop(0)
            if self.mode == "auto":
                approvers.append(who)
                continue
            if not sys.stdin.isatty() and self.ask is input:
                return {**base, "approved": False, "approvers": approvers, "reason": "no interactive terminal"}
            ans = self.ask(f"Approve as {who} (service owner) [{i + 1}/{required}]? [y/N] ").strip().lower()
            if ans not in ("y", "yes"):
                return {**base, "approved": False, "approvers": approvers + [who], "reason": f"rejected by {who}"}
            approvers.append(who)
        if not set(approvers) <= set(owners):
            return {**base, "approved": False, "approvers": approvers, "reason": "approver is not a service owner"}
        return {**base, "approved": True, "approvers": approvers,
                "reason": "simulated on-call approval" if self.mode == "auto" else "approved interactively"}
