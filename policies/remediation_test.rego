# Run with: opa test policies/ -v
package asap.remediation_test

import rego.v1

import data.asap.remediation

base := {
	"action": {"type": "scale", "target": "payments", "params": {"replicas": 8}},
	"target": {"kind": "Deployment", "tier": 1, "replicas": 4, "hpa": {"minReplicas": 4, "maxReplicas": 12}, "pdb": {"minAvailable": 3}},
	"cluster": {"pods_used": 30, "pods_allocatable": 80},
	"dry_run": {"ok": true, "crosses_migration": false},
	"diagnosis": {"confidence": 0.8},
	"evidence": {"valid": true, "has_relevant_metric": true},
	"scope": {"in_incident_scope": true},
	"budget": {"actions_last_30m": 0, "actions_last_24h": 0, "circuit_open": false},
	"controls": {"kill_switch": false, "change_freeze": false},
	"blast_radius": 25,
}

test_scale_up_is_auto if {
	remediation.decision == "allow" with input as base
}

test_rollback_needs_approval if {
	inp := object.union(base, {"action": {"type": "rollback", "target": "checkout", "params": {"to_revision": 2}}})
	remediation.decision == "require_approval" with input as inp
}

test_statefulset_denied if {
	inp := object.union(base, {"action": {"type": "restart", "target": "postgres", "params": {}}, "target": {"kind": "StatefulSet", "tier": 0, "replicas": 3}})
	remediation.decision == "deny" with input as inp
}

test_low_confidence_denied if {
	remediation.decision == "deny" with input as object.union(base, {"diagnosis": {"confidence": 0.4}})
}

test_budget_denied if {
	inp := object.union(base, {"budget": {"actions_last_30m": 1, "actions_last_24h": 1, "circuit_open": false}})
	remediation.decision == "deny" with input as inp
}

test_kill_switch_denied if {
	remediation.decision == "deny" with input as object.union(base, {"controls": {"kill_switch": true, "change_freeze": false}})
}

test_migration_rollback_denied if {
	inp := object.union(base, {
		"action": {"type": "rollback", "target": "checkout", "params": {"to_revision": 2}},
		"dry_run": {"ok": true, "crosses_migration": true},
	})
	remediation.decision == "deny" with input as inp
}

test_irrelevant_evidence_not_auto if {
	remediation.decision == "require_approval" with input as object.union(base, {"evidence": {"valid": true, "has_relevant_metric": false}})
}

test_tier0_needs_two_approvers if {
	remediation.required_approvals == 2 with input as object.union(base, {"target": {"kind": "Deployment", "tier": 0, "replicas": 4, "hpa": {"minReplicas": 4, "maxReplicas": 12}}})
}
