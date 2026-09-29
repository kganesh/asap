"""Treat telemetry as untrusted input.

Log lines, span attributes and label values can be written by an attacker or a buggy service.
Before any tool result reaches the LLM (or the audit store) the gateway:
  * replaces instruction-like strings (prompt-injection attempts) with a marker
  * redacts secrets and credentials
This is defense in depth; the control plane never trusts the agent's choice of target anyway.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

INJECTION_PATTERNS = [
    r"ignore (all |any )?(previous|prior|above) (instructions|prompts?)",
    r"\byou are (now )?(authori[sz]ed|allowed|permitted)\b",
    r"^\s*(system|assistant)\s*:",
    r"['\"]\s*(system|assistant)\s*:",
    r"\bcall (the )?(propose_|submit_|no_action)\w*",
    r"\bdisregard (the )?(rules|policy|guardrails)\b",
]
SECRET_PATTERNS = [
    (r"(?i)(password|passwd|secret|api[_-]?key|token)\s*[=:]\s*\S+", r"\1=[REDACTED]"),
    (r"AKIA[0-9A-Z]{16}", "[REDACTED_AWS_KEY]"),
    (r"(?i)bearer\s+[a-z0-9._\-]{16,}", "Bearer [REDACTED]"),
]
_inj = [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in INJECTION_PATTERNS]


def sanitize(obj: Any, flags: list[str]) -> Any:
    if isinstance(obj, str):
        if any(p.search(obj) for p in _inj):
            digest = hashlib.sha256(obj.encode()).hexdigest()[:12]
            flags.append(digest)
            return f"[REDACTED by gateway: instruction-like content in telemetry, sha256:{digest}]"
        for pat, rep in SECRET_PATTERNS:
            obj = re.sub(pat, rep, obj)
        return obj
    if isinstance(obj, dict):
        return {k: sanitize(v, flags) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize(v, flags) for v in obj]
    return obj
