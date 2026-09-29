"""Settings: one typed source for operational values, one policy source for thresholds, no drift between them."""

from __future__ import annotations

import re
from dataclasses import fields
from pathlib import Path

import pytest

from asap.agent import prompts
from asap.config import ControlSettings, Limits, Settings
from asap.control.plane import Controls
from asap.control.policy import PolicyEngine

from .conftest import needs_policy

PKG = Path(__file__).resolve().parents[1] / "asap"


def test_defaults_do_not_depend_on_the_environment():
    assert Settings.from_env({}) == Settings()


def test_environment_overrides_are_typed():
    s = Settings.from_env({"ASAP_MAX_STEPS": "7", "ASAP_KILL_SWITCH": "true", "ASAP_FLEET_WINDOW_S": "300",
                           "ASAP_TOOL_CHOICE": "any", "OPENAI_BASE_URL": "http://ollama.local/v1"})
    assert s.limits.max_steps == 7
    assert s.control.kill_switch is True and s.control.fleet_window_s == 300.0
    assert s.llm.tool_choice == "any" and s.llm.openai_base_url == "http://ollama.local/v1"
    assert Controls.from_settings(s.control).kill_switch is True


def test_invalid_override_names_the_variable():
    with pytest.raises(ValueError, match="ASAP_MAX_STEPS"):
        Settings.from_env({"ASAP_MAX_STEPS": "lots"})
    with pytest.raises(ValueError, match="ASAP_KILL_SWITCH"):
        Settings.from_env({"ASAP_KILL_SWITCH": "maybe"})


def test_every_variable_name_is_unique():
    names = []
    for section in fields(Settings):
        names += [f.metadata["env"] for f in fields(section.default_factory) if f.metadata.get("env")]  # type: ignore[arg-type]
    assert len(names) == len(set(names))


def test_environment_is_read_only_in_config_or_for_secrets_and_standards():
    """Settings come from Settings.from_env(). The remaining reads are secrets (API keys, simulator
    credentials), the OpenTelemetry standard variable, and `asap doctor` listing which overrides are set."""
    allowed = {"config.py", "agent/llm.py", "sim/api.py", "telemetry/tracing.py", "cli.py"}
    offenders = [str(p.relative_to(PKG)) for p in PKG.rglob("*.py")
                 if re.search(r"os\.environ|os\.getenv", p.read_text()) and str(p.relative_to(PKG)) not in allowed]
    assert not offenders, f"read settings through asap.config, not the environment: {offenders}"


def test_prompt_numbers_come_from_the_limits_and_the_policy():
    text = prompts.system_prompt(Limits(max_replans=5).max_replans, 0.75)
    assert "(max 5)" in text and "confidence below 0.75" in text


@needs_policy
def test_policy_constants_match_what_the_engine_evaluates():
    """Python reads thresholds from the Rego source; the evaluator must agree with that parse."""
    pe = PolicyEngine()
    assert {"min_diagnosis_confidence", "storm_incident_threshold", "fleet_auto_budget"} <= set(pe.constants)
    for name, value in pe.constants.items():
        assert pe.query(f"data.asap.remediation.{name}") == value, name


def test_store_counts_over_the_windows_it_is_given(env):
    """Budget windows are settings; the store only counts, and reports the window alongside the count."""
    from asap.control.store import StateStore

    store = StateStore(env.runs_dir / "w.db")
    store.record_action("p1", "r1", "payments", "scale", 1000.0, "auto", "c/cell")
    short = ControlSettings(budget_short_window_s=60.0)
    counts = store.budget("payments", 1100.0, short.budget_short_window_s, short.budget_long_window_s)
    assert counts["actions_short_window"] == 0 and counts["short_window_minutes"] == 1
    assert counts["actions_long_window"] == 1
