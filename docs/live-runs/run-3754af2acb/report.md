# Incident report: inc-45dc5561af

**Outcome:** `RESOLVED` - SLIs recovered after 5 min  
**Run:** `run-3754af2acb` - reasoner `anthropic:claude-sonnet-5-5` - steps 7, tool calls 6, input tokens 60,461 (peak context 8,615, 0 compaction(s)), output tokens 2,544

## Alerts

| Alert | Service | Severity | Started | Detail |
|---|---|---|---|---|
| HighLatencyP99 | checkout | warning | 2026-09-29T16:46:00Z | p99 1.54s > 1.0s |
| HighLatencyP99 | frontend | warning | 2026-09-29T16:46:00Z | p99 1.43s > 1.0s |
| HighLatencyP99 | payments | warning | 2026-09-29T16:46:00Z | p99 1.61s > 1.0s |
| CPUThrottlingHigh | payments | warning | 2026-09-29T16:46:00Z | CFS throttled 59% > 25% |

## Investigation plan

- Payments CPU saturation (limit too low relative to traffic, or traffic surge) causing CFS throttling and latency that propagates to checkout and frontend; fixable by scaling via HPA
- Recent payments deploy introduced a code/CPU regression (new service.version) causing throttling and latency; fixable by rollback to known-good revision
- Downstream stateful dependency degradation (postgres-inventory, redis-cache) dominating latency, with throttling a secondary effect; no_action and route to the owning team
- Reduced payments replica count or pod restarts/evictions concentrating load on fewer pods

## Diagnosis

Payments is CPU-saturated. Traffic more than doubled at 16:46Z (399 to 923 rps) and the 4 replicas are throttled about 59% by CFS limits. p99 is about 1.6s, and the latency propagates to checkout and frontend. No deploy is involved.

- Root service: `payments` - category `saturation` - confidence 0.9
- Recommended action: `scale`

## Evidence

| ID | Tool | Arguments | Cited |
|---|---|---|---|
| `ev-d4bd97454e` | query_metrics | metric=request_rate, service=payments, window_minutes=60 | yes |
| `ev-409cb83806` | get_deployment_history | service=payments | yes |
| `ev-7c753b71b9` | get_resource_state | service=payments | yes |
| `ev-3a136e08ca` | get_traces | service=payments, window_minutes=15 | yes |
| `ev-2000b2d616` | query_metrics | metric=cpu_throttle_ratio, service=payments, window_minutes=60 | yes |
| `ev-b484d0887f` | query_metrics | metric=replicas_available, service=payments, window_minutes=30 | yes |

## Proposed remediation

`scale` on `payments` with {'replicas': 8} - Payments is CPU-throttled at 59% after traffic rose 2.3x. Raising HPA minReplicas from 4 to 8 adds capacity, and it can be reverted.

## Control plane decision

- Verdict: **allow** (tier 1), blast radius 25, engine `opa (/Users/gkolekar/PycharmProjects/asap/bin/opa)`
- within tier-1 bounds: reversible, capacity-safe, anomalous metric on the target
- Dry-run: ok {'hpa.minReplicas': '4 -> 8', 'replicas': '4 -> 8'}

## Verification

Waited 5 min. Firing before: ['CPUThrottlingHigh', 'HighLatencyP99']; after: none.

## State transitions

1. `TRIAGE` -> `PLAN` incident admitted
1. `PLAN` -> `INVESTIGATE` plan recorded
1. `INVESTIGATE` -> `DIAGNOSE` saturation (confidence 0.90)
1. `DIAGNOSE` -> `PROPOSE` diagnosis accepted
1. `PROPOSE` -> `POLICY_CHECK` scale payments {'replicas': 8}
1. `POLICY_CHECK` -> `EXECUTE` tier 1: within auto-remediation bounds
1. `EXECUTE` -> `VERIFY` applied; waiting for SLI recovery
1. `VERIFY` -> `RESOLVED` SLIs recovered after 5 min

---
Full reasoning trail: `audit.jsonl` (hash-chained). Spans: `spans.jsonl`.
