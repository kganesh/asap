"""Fleet-wide blast radius: budgets across targets, the incident-storm brake, and the fleet lock."""

from __future__ import annotations

import hashlib

from asap.agent.adversarial import run_attacks
from asap.agent.scripted import DeterministicReasoner
from asap.control.approval import ApprovalGate
from asap.control.policy import policy_constant
from asap.harness import run_scenario
from asap.ingest.pipeline import AlertPipeline

from .conftest import needs_policy


@needs_policy
def test_fleet_budget_stops_a_storm_of_individually_valid_auto_actions(env):
    [row] = run_attacks(env, only=["fleet_storm"])
    assert row["executed"] == [True, True, False], row
    assert any("fleet auto-remediation budget" in r for r in row["policy_reasons"]), row["policy_reasons"]
    assert "not approved" in row["blocked_by"]


@needs_policy
def test_incident_storm_pauses_automation(env):
    env.load("cpu_throttle")
    [incident] = env.incidents()
    for n in range(4):  # four other incidents opened in the same cell moments ago
        env.store.record_incident(f"inc-other-{n}", incident.domain, env.server.world.now - 60)
    run, _ = run_scenario(env, "cpu_throttle", DeterministicReasoner(), ApprovalGate("deny"))
    assert run.decision.verdict == "require_approval" and run.execution is None
    assert any("incident storm" in r for r in run.decision.reasons)


def test_fleet_lock_busy_means_no_action(env):
    env.load("cpu_throttle")
    [incident] = env.incidents()
    env.store.acquire_lease(f"fleet:{incident.cluster}", "run-elsewhere", env.server.world.now, ttl_s=600)
    run, _ = run_scenario(env, "cpu_throttle", DeterministicReasoner(), ApprovalGate("auto"))
    assert run.outcome == "REPORT_ONLY" and "fleet lock busy" in run.outcome_reason and run.execution is None


def _alert(service: str, cell: str) -> dict:
    labels = {"alertname": "HighLatencyP99", "service": service, "cluster": "c1", "cell": cell, "severity": "warning"}
    return {"labels": labels, "startsAt": "2026-09-29T16:50:00Z", "status": {"state": "active"},
            "fingerprint": hashlib.sha1(repr(sorted(labels.items())).encode()).hexdigest()[:16]}


def test_many_unrelated_incidents_in_one_cell_are_flagged_as_shared_cause():
    now = 1790701200.0
    deps = {s: [] for s in "abcdef"}  # six services with no known dependencies between them
    alerts = [_alert(s, "cell-a") for s in "abcde"] + [_alert("f", "cell-b")]
    threshold = policy_constant("storm_incident_threshold")  # the pipeline and the policy share one number
    assert threshold == 4
    incidents = AlertPipeline(deps, {}, shared_cause_incidents=threshold).process(alerts, now)
    by_cell = {i.cell: [] for i in incidents}
    for i in incidents:
        by_cell[i.cell].append(i)
    assert len(by_cell["cell-a"]) == 5 and all(i.suspected_shared_cause for i in by_cell["cell-a"])
    assert not by_cell["cell-b"][0].suspected_shared_cause
