# Live-model runs: evidence

One run per scenario against **Claude Sonnet 5.5** (Anthropic API) on 2026-09-30, copied unmodified from `runs/`.
Each folder holds the incident report, the hash-chained audit log (every LLM turn with its reasoning, tool call,
provider token usage and latency; every policy decision with its exact OPA input; the approval; the execution and
verification), and the OpenTelemetry spans.

| Run | Scenario | Outcome | Diagnosis (confidence) | LLM calls | Input tokens (cache reads) |
|---|---|---|---|---|---|
| [run-4d22820b8e](run-4d22820b8e/report.md) | `bad_deploy` | RESOLVED: rollback approved by a human, recovery verified | bad deploy of checkout v1.4.2 (0.95) | 7 | 47.5k (28.2k) |
| [run-3754af2acb](run-3754af2acb/report.md) | `cpu_throttle` | RESOLVED: tier-1 scale-up 4→8, no human, recovery verified | payments CPU saturation after a 2.3x traffic surge (0.90) | 9 | 60.5k (42.6k) |
| [run-992a2d8c39](run-992a2d8c39/report.md) | `db_red_herring` | REPORT_ONLY: routed to the DB team | Postgres lock contention; recent deploy judged non-causal (0.82) | 6 | 38.3k (29.4k) |

Analysis, caveats (one run per scenario is a smoke test, not an evaluation) and what was not exercised live are in
[SYSTEM_DESIGN.md](../../SYSTEM_DESIGN.md#4-cost-and-latency-5000-alerts-per-minute).

## Verify and replay

The logs are tamper-evident: each record hashes the previous one, and each run's final head is in `anchors.jsonl`.

```bash
.venv/bin/asap --runs-dir docs/live-runs verify-audit run-4d22820b8e   # "chain intact" and matches the anchor
.venv/bin/asap --runs-dir docs/live-runs replay run-992a2d8c39         # step-by-step timeline, no LLM calls
```

Editing any record, including this folder's copies, breaks verification. That is why the logs are committed
exactly as produced: the policy-engine field still shows the local absolute path to the OPA binary, which newer
runs no longer record.

The first live attempt (not included) ended REPORT_ONLY with an API 400: Claude Sonnet 5.5 rejects forced tool
choice. That finding and its fix are described in SYSTEM_DESIGN.md and ARCHITECTURE.md.
