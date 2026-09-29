"""End-to-end: each scenario reaches its correct outcome through the full pipeline."""

from __future__ import annotations

from asap.agent.scripted import DeterministicReasoner
from asap.audit.log import verify_chain
from asap.control.approval import ApprovalGate
from asap.harness import run_scenario

from .conftest import needs_policy


@needs_policy
def test_bad_deploy_rolls_back_with_approval(env):
    run, inc = run_scenario(env, "bad_deploy", DeterministicReasoner(), ApprovalGate("auto"))
    assert inc.root_service == "checkout"
    assert run.outcome == "RESOLVED"
    assert run.proposal.action == "rollback" and run.proposal.params == {"to_revision": 2}
    assert run.decision.verdict == "require_approval"
    assert run.approval["approved"] and run.approval["approvers"] == ["alice@checkout-oncall"]
    assert run.flagged_untrusted, "the injected log line must be redacted by the gateway"
    assert env.server.world.workloads["checkout"].current.version == "1.4.1"
    assert verify_chain(env.runs_dir / run.run_id / "audit.jsonl")[0]


@needs_policy
def test_cpu_throttle_scales_automatically(env):
    run, inc = run_scenario(env, "cpu_throttle", DeterministicReasoner(), ApprovalGate("deny"))
    assert inc.root_service == "payments"
    assert run.outcome == "RESOLVED"
    assert run.decision.verdict == "allow" and run.approval is None
    assert env.server.world.workloads["payments"].hpa_min == 8


def test_db_red_herring_does_not_roll_back(env):
    run, inc = run_scenario(env, "db_red_herring", DeterministicReasoner(), ApprovalGate("auto"))
    assert inc.root_service == "inventory"
    assert run.outcome == "REPORT_ONLY"
    assert run.proposal is None
    assert run.diagnosis["root_service"] == "postgres-inventory"
    assert run.replans == 1
    assert env.server.world.workloads["inventory"].current.version == "2.3.1"  # untouched


def test_approval_rejected_means_report_only(env):
    run, _ = run_scenario(env, "bad_deploy", DeterministicReasoner(), ApprovalGate("deny"))
    assert run.outcome == "REPORT_ONLY"
    assert run.execution is None
    assert env.server.world.workloads["checkout"].current.version == "1.4.2"


def test_report_written(env):
    run, _ = run_scenario(env, "db_red_herring", DeterministicReasoner(), ApprovalGate("auto"))
    text = (env.runs_dir / run.run_id / "report.md").read_text()
    assert "postgres-inventory" in text and "REPORT_ONLY" in text
