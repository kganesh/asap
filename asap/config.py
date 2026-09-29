"""Runtime settings: every operational tunable in one typed place.

Three kinds of number appear in ASAP, and each has exactly one home:

1. Operational settings (this module): models, endpoints, timeouts, time windows, lease TTLs, detection
   windows, run caps. The defaults are the values the PoC was built and measured with. Each field names
   the environment variable that overrides it.
2. Policy thresholds (policies/remediation.rego): what may run unattended, budgets, the confidence floor,
   the incident-storm threshold. Python reads them from the policy (`PolicyEngine.constants`) and never
   restates them.
3. Algorithm constants, named next to the code that uses them: the anomaly rule (asap/signals.py),
   blast-radius weights (asap/control/plane.py), the deterministic reasoner's heuristics.

Simulator scenarios and console display widths are fixture and presentation data and stay literal.

Components take their section through the constructor and fall back to the built-in defaults, so tests
never depend on the environment. The environment is read once, by `Settings.from_env()` at the CLI entry
point. Secrets (API keys) are not settings: the provider SDKs read them, and they are never logged.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import Any


def setting(default: Any, env: str | None = None) -> Any:
    """A settings field with a default and, optionally, the environment variable that overrides it."""
    return field(default=default, metadata={"env": env} if env else {})


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def _cast(default: Any, raw: str, name: str) -> Any:
    try:
        if isinstance(default, bool):
            v = raw.strip().lower()
            if v not in _TRUE | _FALSE:
                raise ValueError(raw)
            return v in _TRUE
        if isinstance(default, int):
            return int(raw)
        if isinstance(default, float):
            return float(raw)
        return raw
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not a valid {type(default).__name__}") from None


def _load(cls: type, environ: Mapping[str, str]) -> Any:
    kwargs = {}
    for f in fields(cls):
        name = f.metadata.get("env")
        if name and name in environ:
            kwargs[f.name] = _cast(f.default, environ[name], name)
    return cls(**kwargs)


# --------------------------------------------------------------------------- run caps (the agent loop)
@dataclass(frozen=True)
class Limits:
    """Hard caps on one run. Hitting any of them ends the run as REPORT_ONLY with the evidence so far."""

    max_steps: int = setting(15, "ASAP_MAX_STEPS")  # INVESTIGATE turns
    max_replans: int = setting(2, "ASAP_MAX_REPLANS")
    max_read_calls: int = setting(20, "ASAP_MAX_READ_CALLS")  # read-tool calls, enforced by the gateway
    deadline_s: float = setting(180.0, "ASAP_RUN_DEADLINE_S")
    llm_call_timeout_s: float = setting(60.0, "ASAP_LLM_CALL_TIMEOUT_S")
    max_reevaluations: int = setting(1, "ASAP_MAX_REEVALUATIONS")  # EXECUTE -> POLICY_CHECK after drift
    max_invalid_diagnoses: int = setting(2, "ASAP_MAX_INVALID_DIAGNOSES")
    max_plan_attempts: int = setting(2, "ASAP_MAX_PLAN_ATTEMPTS")
    max_proposal_attempts: int = setting(2, "ASAP_MAX_PROPOSAL_ATTEMPTS")
    max_run_input_tokens: int = setting(200_000, "ASAP_MAX_RUN_INPUT_TOKENS")  # billed input across one run
    compact_at_tokens: int = setting(16_000, "ASAP_COMPACT_AT_TOKENS")  # context size that triggers compaction
    keep_recent_results: int = setting(3, "ASAP_KEEP_RECENT_RESULTS")  # tool results always kept verbatim
    max_result_chars: int = setting(6_000, "ASAP_MAX_RESULT_CHARS")  # one tool result, before truncation


# --------------------------------------------------------------------------- LLM backends
@dataclass(frozen=True)
class LLMSettings:
    model: str = setting("", "ASAP_MODEL")  # overrides the selected backend's default model
    tool_choice: str = setting("", "ASAP_TOOL_CHOICE")  # "any" | "auto"; empty = per-model default
    anthropic_model: str = setting("claude-sonnet-5-5")
    anthropic_fallback_model: str = setting("claude-haiku-4-5-20251001", "ASAP_FALLBACK_MODEL")
    openai_base_url: str = setting("", "OPENAI_BASE_URL")  # empty = OpenAI's public API
    openai_public_url: str = setting("https://api.openai.com/v1")
    openai_model: str = setting("gpt-4.1-mini")
    ollama_base_url: str = setting("http://localhost:11434/v1", "ASAP_OLLAMA_URL")
    ollama_model: str = setting("llama3.1")
    max_output_tokens: int = setting(2_048, "ASAP_MAX_OUTPUT_TOKENS")
    truncated_retry_max_tokens: int = setting(4_096, "ASAP_TRUNCATED_RETRY_MAX_TOKENS")
    sdk_max_retries: int = setting(2, "ASAP_LLM_SDK_RETRIES")
    http_timeout_s: float = setting(60.0, "ASAP_LLM_HTTP_TIMEOUT_S")


# --------------------------------------------------------------------------- control plane
@dataclass(frozen=True)
class ControlSettings:
    # Remediation-budget windows. The limits per window (1 and 3 actions) live in the Rego policy.
    budget_short_window_s: float = setting(1_800.0, "ASAP_BUDGET_SHORT_WINDOW_S")
    budget_long_window_s: float = setting(86_400.0, "ASAP_BUDGET_LONG_WINDOW_S")
    circuit_window_s: float = setting(86_400.0, "ASAP_CIRCUIT_WINDOW_S")
    fleet_window_s: float = setting(600.0, "ASAP_FLEET_WINDOW_S")
    approval_ttl_s: float = setting(900.0, "ASAP_APPROVAL_TTL_S")  # unanswered approval = rejection
    policy_timeout_s: float = setting(5.0, "ASAP_POLICY_TIMEOUT_S")
    policy_dir: str = setting("", "ASAP_POLICY_DIR")  # empty = the bundled policies/ directory
    opa_bin: str = setting("", "ASAP_OPA_BIN")  # empty = ./bin/opa, then PATH
    kill_switch: bool = setting(False, "ASAP_KILL_SWITCH")  # every action denied
    change_freeze: bool = setting(False, "ASAP_CHANGE_FREEZE")  # every action needs approval


# --------------------------------------------------------------------------- execution
@dataclass(frozen=True)
class ExecutionSettings:
    incident_lease_ttl_s: float = setting(900.0, "ASAP_INCIDENT_LEASE_TTL_S")
    target_lease_ttl_s: float = setting(900.0, "ASAP_TARGET_LEASE_TTL_S")
    fleet_lock_ttl_s: float = setting(60.0, "ASAP_FLEET_LOCK_TTL_S")  # held for seconds: re-validate + apply
    fleet_lock_wait_attempts: int = setting(30, "ASAP_FLEET_LOCK_WAIT_ATTEMPTS")
    fleet_lock_wait_pause_s: float = setting(0.1, "ASAP_FLEET_LOCK_WAIT_PAUSE_S")
    verify_wait_minutes: int = setting(5, "ASAP_VERIFY_WAIT_MINUTES")  # SLI recovery window after apply


# --------------------------------------------------------------------------- alert ingestion
@dataclass(frozen=True)
class IngestSettings:
    """Funnel windows. The shared-cause threshold is the policy's `storm_incident_threshold`."""

    debounce_s: float = setting(120.0, "ASAP_DEBOUNCE_S")  # matches the alert rules' `for: 2m`
    flap_limit: int = setting(2, "ASAP_FLAP_LIMIT")  # state flips within flap_window_s
    flap_window_s: float = setting(600.0, "ASAP_FLAP_WINDOW_S")
    dedup_window_s: float = setting(300.0, "ASAP_DEDUP_WINDOW_S")
    correlation_window_s: float = setting(600.0, "ASAP_CORRELATION_WINDOW_S")


# --------------------------------------------------------------------------- CLI
@dataclass(frozen=True)
class AppSettings:
    runs_dir: str = setting("runs", "ASAP_RUNS_DIR")
    log_level: str = setting("WARNING", "ASAP_LOG_LEVEL")


@dataclass(frozen=True)
class Settings:
    limits: Limits = field(default_factory=Limits)
    llm: LLMSettings = field(default_factory=LLMSettings)
    control: ControlSettings = field(default_factory=ControlSettings)
    execution: ExecutionSettings = field(default_factory=ExecutionSettings)
    ingest: IngestSettings = field(default_factory=IngestSettings)
    app: AppSettings = field(default_factory=AppSettings)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """Built-in defaults, overridden by any ASAP_* (and OPENAI_BASE_URL) variables that are set."""
        env = os.environ if environ is None else environ
        return cls(**{f.name: _load(f.default_factory, env) for f in fields(cls)})  # type: ignore[misc]

    def env_vars(self) -> dict[str, str]:
        """Every supported environment variable and its current value (for `asap doctor` and the docs)."""
        out = {}
        for f in fields(self):
            section = getattr(self, f.name)
            for sf in fields(section):
                if sf.metadata.get("env"):
                    out[sf.metadata["env"]] = str(getattr(section, sf.name))
        return out
