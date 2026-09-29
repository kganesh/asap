# Incident report: inc-e6e86a09d6

**Outcome:** `RESOLVED` - SLIs recovered after 5 min  
**Run:** `run-4d22820b8e` - reasoner `anthropic:claude-sonnet-5-5` - steps 5, tool calls 3, input tokens 47,517 (peak context 5,791, 0 compaction(s)), output tokens 2,405

## Alerts

| Alert | Service | Severity | Started | Detail |
|---|---|---|---|---|
| HighErrorRate | checkout | critical | 2026-09-29T16:49:30Z | 5xx ratio 18.2% > 5% |
| HighErrorRate | frontend | critical | 2026-09-29T16:49:30Z | 5xx ratio 9.3% > 5% |

## Investigation plan

- Bad deploy: checkout revision 3 introduced a code defect causing 5xx (new exceptions tagged with new service.version)
- Downstream dependency degradation (payments, inventory/postgres-inventory, redis-cache) causing checkout errors that propagate to frontend
- Saturation on checkout (CPU throttling, traffic surge, pod restarts/OOM) causing 5xx
- Frontend-originated issue, with checkout only a secondary victim

## Diagnosis

Checkout v1.4.2 (revision 3, promo-code resolver PR #4812, deployed 16:48Z) throws NullPointerException in PromoCodeResolver.apply (line 88) when promoCode is null. This causes 500s on POST /checkout/submit, which propagate to frontend as 5xx.

- Root service: `checkout` - category `bad_deploy` - confidence 0.95
- Recommended action: `rollback`

## Evidence

| ID | Tool | Arguments | Cited |
|---|---|---|---|
| `ev-017560670a` | get_deployment_history | service=checkout | yes |
| `ev-f91fc852de` | search_logs | service=checkout, level=ERROR, pattern=None, window_minutes=15, limit=8 | yes |
| `ev-cda55b6e31` | get_traces | service=checkout, window_minutes=15 | yes |

## Proposed remediation

`rollback` on `checkout` with {'to_revision': 2} - Roll checkout back to known-good rev 2 (v1.4.1) to stop NPE-driven 5xx introduced by v1.4.2's promo-code resolver.

## Control plane decision

- Verdict: **require_approval** (tier 2), blast radius 40, engine `opa (/Users/gkolekar/PycharmProjects/asap/bin/opa)`
- rollback changes running code
- unattended actions need a cited metric on the target that shows the anomaly
- Dry-run: ok {'image': 'registry.internal/shop/checkout:1.4.2 -> registry.internal/shop/checkout:1.4.1', 'via': 'git revert + Argo CD sync', 'reverts_revisions': [3]}

## Approval

Approved by ['alice@checkout-oncall'] via cli (stand-in for Slack interactive message + PagerDuty note) - approved interactively

## Verification

Waited 5 min. Firing before: ['HighErrorRate']; after: none.

## State transitions

1. `TRIAGE` -> `PLAN` incident admitted
1. `PLAN` -> `INVESTIGATE` plan recorded
1. `INVESTIGATE` -> `DIAGNOSE` bad_deploy (confidence 0.95)
1. `DIAGNOSE` -> `PROPOSE` diagnosis accepted
1. `PROPOSE` -> `POLICY_CHECK` rollback checkout {'to_revision': 2}
1. `POLICY_CHECK` -> `AWAIT_APPROVAL` rollback changes running code; unattended actions need a cited metric on the target that shows the anomaly
1. `AWAIT_APPROVAL` -> `EXECUTE` approved by alice@checkout-oncall
1. `EXECUTE` -> `VERIFY` applied; waiting for SLI recovery
1. `VERIFY` -> `RESOLVED` SLIs recovered after 5 min

---
Full reasoning trail: `audit.jsonl` (hash-chained). Spans: `spans.jsonl`.
