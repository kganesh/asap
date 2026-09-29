"""Human-in-the-loop approval gate.

Production: a Slack interactive message (signed request) plus a PagerDuty incident note; approver
identity is checked against service ownership in the catalog; tier-0 targets need two approvers;
the approval is bound to a hash of the target's state and voided if that state changes.

PoC: the same rules, with a CLI prompt standing in for Slack. The TTL is enforced: no answer within
`ttl_seconds` is a rejection (escalate), never an approval. Approver identity is SIMULATED here -
whoever answers the prompt answers as the listed owner - which a real Slack integration replaces.
"""

from __future__ import annotations

import hashlib
import logging
import sys
import threading
from collections.abc import Callable

from ..config import ControlSettings

log = logging.getLogger(__name__)


def state_hash(target: str, resource_version: str | None, params: dict) -> str:
    return hashlib.sha256(f"{target}|{resource_version}|{sorted(params.items())}".encode()).hexdigest()[:16]


class _NoAnswer(Exception):
    pass


def ask_with_timeout(ask: Callable[[str], str], prompt: str, timeout_s: float) -> str:
    """Run a blocking prompt with a deadline. EOF and Ctrl-C count as 'no'; a timeout raises _NoAnswer."""
    result: dict[str, object] = {}

    def target() -> None:
        try:
            result["answer"] = ask(prompt)
        except (EOFError, KeyboardInterrupt) as e:
            result["error"] = e
        except Exception as e:  # noqa: BLE001 - any prompt failure is a non-approval
            result["error"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    try:
        t.join(timeout_s)
    except KeyboardInterrupt:
        return "n"
    if t.is_alive():
        raise _NoAnswer()
    if "error" in result:
        log.info("approval prompt ended without an answer: %r", result["error"])
        return "n"
    return str(result.get("answer", ""))


class ApprovalGate:
    """mode: prompt | auto | deny | timeout"""

    def __init__(self, mode: str = "auto", render: Callable[[dict], None] | None = None,
                 ask: Callable[[str], str] | None = None, ttl_seconds: float | None = None) -> None:
        self.mode = mode
        self.render = render or (lambda packet: None)
        self.ask = ask or input
        self.ttl_seconds = ControlSettings().approval_ttl_s if ttl_seconds is None else ttl_seconds

    def request(self, packet: dict, owners: list[str], required: int) -> dict:
        self.render(packet)
        base = {"channel": "cli (stand-in for Slack interactive message + PagerDuty note)",
                "ttl_seconds": self.ttl_seconds, "required_approvals": required,
                "state_hash": packet["state_hash"]}
        timeout_reason = f"no response within {self.ttl_seconds / 60:g} min; escalated (never auto-approved)"
        if self.mode == "timeout":
            return {**base, "approved": False, "approvers": [], "reason": timeout_reason}
        if self.mode == "deny":
            return {**base, "approved": False, "approvers": owners[:1], "reason": "rejected by approver"}
        if len(set(owners)) < required:
            return {**base, "approved": False, "approvers": [],
                    "reason": f"needs {required} distinct owners; service catalog lists {len(set(owners))}"}
        approvers: list[str] = []
        for i, who in enumerate(owners[:required]):
            if self.mode == "auto":
                approvers.append(who)
                continue
            if self.ask is input and not sys.stdin.isatty():
                return {**base, "approved": False, "approvers": approvers, "reason": "no interactive terminal"}
            prompt = f"Approve as {who} (service owner) [{i + 1}/{required}]? [y/N] "
            try:
                ans = ask_with_timeout(self.ask, prompt, self.ttl_seconds).strip().lower()
            except _NoAnswer:
                log.warning("approval TTL expired for %s", packet.get("proposal", {}).get("target"))
                return {**base, "approved": False, "approvers": approvers, "reason": timeout_reason}
            if ans not in ("y", "yes"):
                return {**base, "approved": False, "approvers": approvers + [who], "reason": f"rejected by {who}"}
            approvers.append(who)
        return {**base, "approved": True, "approvers": approvers,
                "reason": "simulated on-call approval" if self.mode == "auto" else "approved interactively"}
