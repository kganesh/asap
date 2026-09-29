# ASAP remediation policy - the single source of truth for what may run unattended.
#
# Input is assembled by the control plane from sources it fetches itself (never from the
# agent's proposal text): target state, cluster capacity, remediation budget, dry-run result,
# blast-radius score and global controls.
#
# Output: decision = "allow" (tier 1, auto) | "require_approval" (tier 2) | "deny" (tier 3).
# Anything not explicitly allowed defaults to deny.
package asap.remediation

import rego.v1

allowed_actions := {"rollback", "scale", "restart", "cache_flush"}

# ---------------------------------------------------------------- tier 3: deny
deny contains "action type is not in the allowlist" if {
	not input.action.type in allowed_actions
}

deny contains "kill switch engaged: ASAP is in report-only mode" if {
	input.controls.kill_switch == true
}

deny contains "target is not a Deployment; stateful workloads are never remediated automatically" if {
	input.action.type != "cache_flush"
	input.target.kind != "Deployment"
}

deny contains "dry-run failed" if {
	input.dry_run.ok != true
}

deny contains "rollback crosses a schema migration" if {
	input.action.type == "rollback"
	input.dry_run.crosses_migration == true
}

deny contains sprintf("diagnosis confidence %v is below 0.6", [input.diagnosis.confidence]) if {
	input.diagnosis.confidence < 0.6
}

deny contains "evidence IDs are missing or were never issued in this run" if {
	input.evidence.valid != true
}

deny contains "target is outside the incident's dependency scope" if {
	input.scope.in_incident_scope != true
}

deny contains "remediation budget: max 1 action per target per 30 minutes" if {
	input.budget.actions_last_30m >= 1
}

deny contains "remediation budget: max 3 actions per target per day" if {
	input.budget.actions_last_24h >= 3
}

deny contains "circuit breaker open: this action already failed to improve this target" if {
	input.budget.circuit_open == true
}

deny contains "scaling is only done through an HPA" if {
	input.action.type == "scale"
	not has_hpa
}

deny contains "scale target exceeds HPA maxReplicas" if {
	input.action.type == "scale"
	input.action.params.replicas > input.target.hpa.maxReplicas
}

deny contains "insufficient cluster capacity for scale-up" if {
	input.action.type == "scale"
	extra := input.action.params.replicas - input.target.replicas
	input.cluster.pods_used + extra > input.cluster.pods_allocatable
}

has_hpa if input.target.hpa.maxReplicas

# ---------------------------------------------------------------- tier 2: human approval
require_approval contains "rollback changes running code" if {
	input.action.type == "rollback"
}

require_approval contains "cache flush can shift load onto the database" if {
	input.action.type == "cache_flush"
}

require_approval contains "scale-down reduces capacity" if {
	input.action.type == "scale"
	input.action.params.replicas <= input.target.replicas
}

require_approval contains "tier-0 service" if {
	input.target.tier == 0
}

require_approval contains "change freeze in effect" if {
	input.controls.change_freeze == true
}

require_approval contains sprintf("blast radius %v >= 50", [input.blast_radius]) if {
	input.blast_radius >= 50
}

require_approval contains "restart would breach the PodDisruptionBudget or leave < 30% cluster headroom" if {
	input.action.type == "restart"
	not restart_safe
}

require_approval contains "unattended actions need a cited metric on the target that shows the anomaly" if {
	input.evidence.has_relevant_metric != true
}

restart_safe if {
	input.target.replicas >= 3
	input.target.replicas - 1 >= input.target.pdb.minAvailable
	# cluster headroom > 30%, in integer arithmetic
	free_pods := input.cluster.pods_allocatable - input.cluster.pods_used
	free_pods * 10 > input.cluster.pods_allocatable * 3
}

# ---------------------------------------------------------------- decision
default decision := "deny"

decision := "deny" if {
	count(deny) > 0
} else := "require_approval" if {
	count(require_approval) > 0
} else := "allow"

default required_approvals := 1

required_approvals := 2 if input.target.tier == 0
