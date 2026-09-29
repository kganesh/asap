"""Policy-as-code evaluation. Rego is the single source of truth; two evaluators can run it:

1. the `opa` binary (on PATH, or ./bin/opa installed by `make setup`)  - preferred
2. `regopy` (Microsoft rego-cpp Python bindings)                        - fallback

If neither is available, or evaluation errors or times out, the engine raises
PolicyUnavailable and the control plane FAILS CLOSED (deny).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

QUERY = ("[data.asap.remediation.decision, data.asap.remediation.deny, "
         "data.asap.remediation.require_approval, data.asap.remediation.required_approvals]")


class PolicyUnavailable(Exception):
    pass


def policy_dir() -> Path:
    env = os.environ.get("ASAP_POLICY_DIR")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for cand in (here.parents[2] / "policies", here.parents[1] / "_policies"):
        if (cand / "remediation.rego").exists():
            return cand
    raise PolicyUnavailable("policy bundle not found")


def _find_opa() -> str | None:
    env = os.environ.get("ASAP_OPA_BIN")
    if env and Path(env).exists():
        return env
    local = Path(__file__).resolve().parents[2] / "bin" / "opa"
    if local.exists():
        return str(local)
    return shutil.which("opa")


class PolicyEngine:
    def __init__(self, engine: str = "auto", timeout_s: float = 5.0) -> None:
        self.timeout_s = timeout_s
        self.forced_unavailable = False  # test hook: simulate OPA outage
        self.opa = _find_opa() if engine in ("auto", "opa") else None
        self.regopy = None
        if self.opa is None and engine in ("auto", "regopy"):
            try:
                import regopy  # noqa: F401

                self.regopy = regopy
            except ImportError:
                self.regopy = None
        try:
            self.path = policy_dir() / "remediation.rego"
            self.source = self.path.read_text()
            self.digest = "sha256:" + hashlib.sha256(self.source.encode()).hexdigest()[:16]
        except (PolicyUnavailable, OSError):
            self.path, self.source, self.digest = None, None, "unavailable"

    @property
    def name(self) -> str:
        if self.opa:
            # Relative to the repo when bundled, else just the binary name: audit logs and reports get shared, and
            # an absolute path leaks the local username and directory layout.
            repo = Path(__file__).resolve().parents[2]
            p = Path(self.opa)
            shown = str(p.relative_to(repo)) if p.is_absolute() and repo in p.parents else p.name
            return f"opa ({shown})"
        if self.regopy:
            return "regopy (rego-cpp)"
        return "none (fail-closed)"

    def evaluate(self, policy_input: dict) -> dict:
        if self.forced_unavailable or self.source is None:
            raise PolicyUnavailable("policy engine unreachable")
        if self.opa:
            return self._eval_opa(policy_input)
        if self.regopy:
            return self._eval_regopy(policy_input)
        raise PolicyUnavailable("no Rego evaluator installed (run `make setup` or `pip install asap[rego]`)")

    def _eval_opa(self, policy_input: dict) -> dict:
        try:
            p = subprocess.run([self.opa, "eval", "--format=json", "--stdin-input", "-d", str(self.path), QUERY],
                               input=json.dumps(policy_input), capture_output=True, text=True,
                               timeout=self.timeout_s, check=True)
            value = json.loads(p.stdout)["result"][0]["expressions"][0]["value"]
        except (subprocess.SubprocessError, OSError, KeyError, IndexError, json.JSONDecodeError) as e:
            raise PolicyUnavailable(f"opa evaluation failed: {e}") from e
        return _shape(value)

    def _eval_regopy(self, policy_input: dict) -> dict:
        try:
            interp = self.regopy.Interpreter()  # type: ignore[union-attr]
            interp.add_module("remediation.rego", self.source)
            interp.set_input_term(json.dumps(policy_input))
            value = json.loads(str(interp.query(QUERY)))["expressions"][0]
        except Exception as e:  # noqa: BLE001 - any evaluator failure must fail closed
            raise PolicyUnavailable(f"regopy evaluation failed: {e}") from e
        return _shape(value)


def _shape(value: list) -> dict:
    decision, deny, approve, approvals = value
    if decision not in ("allow", "require_approval", "deny"):
        raise PolicyUnavailable(f"unexpected decision {decision!r}")
    return {"decision": decision, "deny": sorted(deny), "require_approval": sorted(approve),
            "required_approvals": int(approvals)}
