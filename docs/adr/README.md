# Architecture Decision Records

Each record captures one decision: the context, the options considered, what was chosen, and the costs accepted. The status marks whether the decision is **built** in this PoC or is **production design** only, matching the [built] / [design] tags in ARCHITECTURE.md and SYSTEM_DESIGN.md.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-llm-proposes-control-plane-decides.md) | The LLM only proposes; a deterministic control plane decides and acts | Built |
| [0002](0002-plan-and-execute-with-bounded-react.md) | Plan-and-Execute with a bounded ReAct inner loop | Built |
| [0003](0003-custom-state-machine-over-agent-framework.md) | Custom state machine rather than LangGraph / AutoGen / CrewAI | Built |
| [0004](0004-policy-as-code-in-rego.md) | Remediation policy in OPA/Rego, evaluated fail-closed | Built |
| [0005](0005-fail-open-detection-fail-closed-execution.md) | Detection fails open, execution fails closed; ASAP sits beside paging | Built (paging path: design) |
| [0006](0006-linearizable-leases-and-budgets.md) | Leases, budgets and approvals in a linearizable store; deny under partition | Built on SQLite; production store: design |
| [0007](0007-kafka-keyed-by-failure-domain.md) | Alert stream keyed by failure domain, not by service | Funnel built; Kafka: design |
| [0008](0008-remediate-through-controllers.md) | Roll back through GitOps and scale through the HPA | Built (simulated Argo CD) |
| [0009](0009-deterministic-reasoner-fallback.md) | A deterministic reasoner when no LLM key is configured | Built |
| [0010](0010-context-caching-over-compaction.md) | Cache the conversation prefix; compact only as a safety valve; per-run token budget | Built |
| [0011](0011-fleet-wide-blast-radius.md) | Fleet-wide blast radius: per-domain auto budget, incident-storm brake, fleet lock | Built |
| [0012](0012-one-home-per-number.md) | One home per number: typed settings, policy constants, named algorithm constants | Built |

New decisions get the next number. A superseded ADR stays in place with its status changed to "Superseded by ADR-NNNN".
