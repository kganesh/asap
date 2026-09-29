# ASAP: Production Readiness and Failure Analysis

Principle: **detection fails open, execution fails closed.** If any part of ASAP breaks, humans are paged exactly as they would be without it, and nothing changes in production.

Each claim below is tagged **[built]** (implemented and tested in this repo, with the test name) or **[design]** (production design, not built in the PoC).

---

## 1. Safety and guardrails: preventing "hallucinated remediation"

Safety is layered. Each layer is deterministic and independently testable, and the LLM sits entirely upstream of all of them. Nothing relies on the prompt; `asap/agent/prompts.py` says so explicitly.

| # | Layer | What it stops | Where / test |
|---|---|---|---|
| 1 | **Closed action space.** Four action tools, no shell/kubectl/SQL; Pydantic `extra="forbid"` | "Drop the database" is not expressible; unknown tools are rejected | `tools/schemas.py`; attack `shell_tool` **[built]** |
| 2 | **Evidence binding.** Diagnoses and proposals must cite `evidence_id`s the gateway issued *in this run* | Hallucinated justification | attack `fabricated_evidence` **[built]** |
| 3 | **Scope derived by the control plane.** Target must be the incident's root service or downstream of it, computed from the dependency graph | Acting on victims or on a target named by an injected string | attacks `victim_target`, `prompt_injection` **[built]** |
| 4 | **Untrusted-telemetry sanitizer.** Instruction-like strings and secrets redacted before the LLM sees them | Prompt injection via logs, headers, span attributes | `tools/sanitize.py`; `test_bad_deploy_rolls_back_with_approval` **[built]** |
| 5 | **Dry-run.** Diff + precondition `resourceVersion`; a failed dry-run means deny | Nonexistent revisions, replicas over HPA max, rollback to the running version | attacks `nonexistent_revision`, `scale_beyond_hpa` **[built]** |
| 6 | **Policy-as-code (OPA/Rego).** `policies/remediation.rego`; default deny; unit-tested with `opa test` | StatefulSets/DBs, migrations, low confidence, missing HPA, capacity, freeze, kill switch | `make test`; attack `drop_database` **[built]** |
| 7 | **Remediation budget + circuit breaker.** Max 1 action per target per 30 min, 3/day; same action that didn't help opens the breaker | **Infinite restart loops** | attack `restart_loop`: run 1 executes and escalates, runs 2-3 denied **[built]** |
| 8 | **State-machine limits.** Legal-transition table; 15 steps, 2 replans, 2 invalid diagnoses, 1 re-evaluation, 180 s deadline | Endless investigation; skipping the control plane (PROPOSE → EXECUTE is illegal) | attack `endless_investigation`; `test_illegal_transitions_are_rejected` **[built]** |
| 9 | **Re-validation at execute time.** Fresh dry-run + fresh policy; drift voids the approval | Time-of-check vs time-of-use (TOCTOU): acting on state that changed during approval | `test_drift_after_approval_voids_it_and_reevaluates` **[built]** |
| 10 | **Credential separation.** Only the executor holds write credentials | The agent calling write APIs directly | `test_agent_credential_cannot_write` **[built]** |
| 11 | **Fail closed.** No policy engine, OPA error or timeout, store unavailable: deny | Guardrail outages becoming open doors | attacks `policy_engine_down`, `test_failing_closed_without_any_policy_engine`, `test_state_store_outage_fails_closed` **[built]** |
| 12 | **Kill switch / change freeze.** `ASAP_KILL_SWITCH=1` forces report-only; freeze forces approval | Operator override during an incident or freeze | attack `kill_switch` **[built]** |

`make attack` runs 13 hostile scripted models against these layers; each must end with no unsafe action, and the table shows which layer stopped it.

---

## 2. Blast radius and human-in-the-loop

### Risk tiers (decided by Rego, not by the model)

| Tier | Examples | Gate | Why |
|---|---|---|---|
| 0: read | Any telemetry query, within per-run quotas | None | No side effects beyond query cost |
| **1: auto** | Scale **up** via HPA `minReplicas` within HPA max and cluster capacity; restart a stateless Deployment only if its PDB allows it and cluster headroom > 30% | Policy allow + dry-run + budget + re-validation + **metric evidence** | Reversible, bounded, capacity-safe |
| **2: approve** | Rollback (as a GitOps revert); cache flush (by key prefix); scale down; anything on a tier-0 service (**2 approvers**); anything during a change freeze; blast radius ≥ 50; log-only evidence | Human approval by an **authorized service owner**, bound to a state hash, 15-min TTL, never auto-approved | Reversible but user-visible, or can cause a thundering herd |
| **3: deny / report** | StatefulSets, databases; rollback across a schema migration; confidence < 0.6; invalid evidence; out-of-scope target; budget or breaker; kill switch | Never executed; incident report + recommendation | Irreversible, or the diagnosis is too weak |

Cache flush is tier 2 on purpose: flushing a hot cache under load shifts that load to the database and can turn a latency incident into an outage.

### Blast-radius score (`control/plane.py`)

`score = tier weight (tier0 40 / tier1 20 / tier2 10) + reversibility (scale-up 0, restart 10, rollback 15, scale-down 15, cache flush 25) + 5 × upstream dependents (max 20) + 15 if every pod is cycled`. A score ≥ 50 requires approval. Examples from the demo: `payments` scale-up = 25 (auto); `checkout` rollback = 40 (approval, because rollback is always tier 2); `checkout` restart = 50 (approval).

### Approval flow

- **[built]** Approval packet: diagnosis, cited evidence, plan, dry-run diff, policy reasons, blast radius, required approvers, state hash. Approvers must be listed owners in the service catalog; tier-0 targets need two distinct owners. Timeout escalates and never auto-approves (attack `approval_timeout`). Rejection means report-only.
- **[design]** Slack interactive message (signed request, identity mapped to the service catalog) + PagerDuty incident note; rubber-stamp detection (approval latency < 10 s tracked in ops review).

### Partial failure, idempotency and rollback

| Situation | Behavior |
|---|---|
| Retry / redelivery of the same proposal | Executor records `(proposal_id, step)` before and after apply; a repeat returns `idempotent_replay` **[built]** `test_executor_is_idempotent_per_proposal` |
| Executor crash between apply and bookkeeping | On restart the step is `started`: reconcile from *observed* state and never re-apply blindly **[built]** `test_executor_crash_after_apply_is_reconciled_not_reapplied` |
| Two runs targeting the same workload | Lease per target (and per incident) in the store; the second reports **[built]** `test_duplicate_incident_delivery_is_ignored` |
| Cluster controllers undoing the fix | Rollback = GitOps revert synced by Argo CD, not a direct patch; scaling = HPA `minReplicas`, because HPA would overwrite `replicas` **[built]** in the executor + simulator |
| Rollout stuck at 50% (`progressDeadlineExceeded`) | Stop, record both revisions and the pod split, escalate; no automatic forward retry **[design]** |
| No recovery after the verify window | **Per-action revert rules**: scale → restore previous `minReplicas`; restart → nothing to revert; cache flush → cannot be undone; **rollback → never auto-revert** (it would redeploy the bad version). Then escalate **[built]** `Executor.revert` |
| Alert self-resolves mid-run | "Still firing?" gate before propose and before execute **[built]** `test_alert_resolved_before_investigation` |

---

## 3. Observability and auditability

Every run is one OpenTelemetry trace plus an append-only, hash-chained audit log. A post-mortem can replay the run without the LLM.

**Trace** (`runs/<run>/spans.jsonl`; OTLP if `OTEL_EXPORTER_OTLP_ENDPOINT` is set) **[built]**:
`asap.incident` → `asap.state.TRIAGE`, `gen_ai.chat` (model, input/output tokens, tool chosen), `asap.tool.<name>` (args, evidence_id, rejection), `asap.policy.eval` (verdict, tier), `asap.approval`, `asap.execute`, `asap.verify`.

**Audit record** (`runs/<run>/audit.jsonl`) **[built]**:

```json
{"run_id": "run-…", "seq": 17, "ts": 1790701234.5, "sim_time": 1790701200.0,
 "actor": "agent|policy|human|executor|system", "event": "llm_turn|tool_result|diagnosis|proposal|decision|approval|applied|verification|transition|…",
 "payload": {"…": "full inputs and outputs, including the exact OPA input document"},
 "versions": {"asap": "0.1.0", "model": "claude-sonnet-5-5", "prompt": "asap-sre-2026-09-29.1",
              "tool_schema": "2026-09-29.1", "policy_bundle": "sha256:…", "policy_engine": "opa"},
 "prev_hash": "…", "hash": "sha256(canonical record)"}
```

- **Why did it do that?** The `llm_turn` records hold the model's stated thought and tool call; `decision` holds the exact policy input and the rules that fired; `approval` holds who approved which state hash. `asap replay <run_id>` prints the timeline and verifies the chain.
- **Tamper evidence:** the chain detects edits; the head is appended to `runs/anchors.jsonl` so removed or appended records are detected too **[built]** `test_audit_tampering_is_detected`. **[design]** Anchors go to WORM storage (S3 Object Lock, compliance mode).
- **Reproducibility:** model, prompt, tool-schema and policy-bundle versions are on every record, so behavior can be attributed after an upgrade.
- **Data governance [design]:** secrets and PII are redacted at the gateway before storage (**[built]** for secrets and injection patterns); retention of 90 days for full prompts and 7 years for decision records, mapped to SOC 2 CC7/CC8 and FedRAMP AU controls.

**Agent metrics** (`runs/metrics.prom`, Prometheus exposition) **[built]**: `asap_runs_total{outcome}`, `asap_actions_total{action,tier,verdict}`, `asap_policy_denials_total{reason}`, `asap_llm_tokens_total{model,direction}`, `asap_tool_calls_rejected_total{reason}`, `asap_remediation_reverted_total{action}`, `asap_time_to_diagnosis_seconds`.

**ASAP's own SLOs [design]** (these page the ASAP team, not service owners):

| SLI | Objective |
|---|---|
| P1 incident → diagnosis | 95% < 3 min |
| Executed actions later reverted or judged wrong | < 2% / 30 days |
| Policy-denied proposals | alert on a 3x week-over-week jump (model or prompt regression) |
| P1 queue age | 99% < 30 s |
| Audit anchor lag | < 5 min |

Model, prompt or policy changes ship only if the scenario and adversarial suites pass (`make test`, `make attack`; CI runs both).

---

## 4. Cost and latency: 5,000 alerts per minute

5,000 alerts must never become 5,000 LLM runs. A deterministic funnel collapses the storm before any token is spent, and the LLM budget is enforced like any other quota.

**Measured** (`make storm`, [built]):

| Stage | Removed | Remaining |
|---|---|---|
| received | | 5,000 |
| resolved notifications | 120 | 4,880 |
| flapping (≥ 2 state flips / 10 min) | 135 | 4,745 |
| debounced (`for: 2m`) | 2,495 | 2,250 |
| duplicate fingerprints (5-min window) | 2,184 | 66 |
| grouped by service + alertname | | 8 groups |
| correlated via dependency graph | | **2 incidents → 2 LLM runs** |

Correlation roots each incident at the *deepest failing dependency* within a failure domain (cluster + cell), so a cascade (`checkout` → `frontend`) is one incident rooted at `checkout`. Incident IDs are deterministic (hash of domain, root and window), so redelivered alerts map to the same incident.

**Production pipeline [design]:**

1. Alertmanager → Kafka `alerts.raw`, **keyed by failure domain** (not service), so a cross-service cascade lands on one partition and one correlator. Trade-off: hot partitions for large cells, mitigated by sub-keying on namespace when lag breaches its SLO.
2. Stream processor (Flink / Kafka Streams) holds dedup and correlation windows in **changelog-backed state**, so a restart restores windows instead of re-opening the storm.
3. **Rules before reasoning:** known signatures (for example, a deploy in the last 15 minutes plus errors on the new version) are attached as runbook hints; the LLM confirms. **[built]** as hints.
4. **Priority queue** by severity × tier (**[built]** scoring), with a queue-age SLO (P1 < 30 s, P3 < 10 min).
5. **Bounded worker pool + token buckets per model**, with **30% of tokens reserved for P1**; exponential backoff with jitter on 429s.
6. **Per-run caps** (**[built]**): 15 steps, 20 read calls, 2 replans, 180 s deadline. Hitting a cap produces a report, not a retry.
7. **Load shedding:** when queue age breaches its SLO, P3/P4 incidents fall back to rules-only reports; shedding is counted, never silent.
8. **Global token budget / provider outage:** fall back to rules-only triage that **never auto-remediates**; it only annotates pages.

**Capacity model** (assumptions stated so they can be challenged; validate with load tests):

| Quantity | Assumption | Derived |
|---|---|---|
| Compression after the funnel | 500-2,500x in a cascade (measured: 2,500x) | 2-10 incidents / min |
| Mean active run time (excluding approval waits) | ~90 s | 0.67 runs / worker / min |
| Workers at 10 incidents / min | 10 ÷ 0.67 | 15, provision 20 |
| Tokens per run | ~40k in (cached system prompt + schemas), ~4k out | ~440k tokens / min at peak; size the provider tier to 2x |

**Token cost levers:** prompt caching on the static system prompt and tool schemas (**[built]** `cache_control`); tool results summarized server-side (percentiles and change points, clustered log templates, not raw lines) (**[built]**); smaller model for triage and larger model for root-cause analysis (RCA) (**[design]**); reuse a recent diagnosis for a re-fired incident while its evidence is fresh (**[design]**).

**LLM latency spike (for example, a 30 s stall on a P1):** every call's timeout comes from the run's remaining deadline; on timeout, 429 or 5xx the Anthropic adapter hedges to a fallback model (`claude-haiku-4-5`), then ends as report-only with the evidence so far (**[built]** `test_anthropic_adapter_falls_back_then_gives_up`, `test_llm_outage_degrades_to_report_only`, `test_deadline_ends_run_with_report`). Because ASAP sits beside paging, a stall delays ASAP's annotation, never the page.

**Latency targets:** ingest → incident < 10 s p99; incident → diagnosis < 3 min p95 for P1 under storm load.

---

## 5. Failure modes and consistency

| Dependency down or degraded | Mode | Behavior |
|---|---|---|
| LLM provider (errors, 429s, latency) | degrade | Hedge to the fallback model, then rules-only report; paging unaffected **[built]** |
| OPA / policy bundle | **fail closed** | No actions; report-only **[built]** |
| Lease + budget store | **fail closed** | No actions **[built]** |
| Kafka / correlator | degrade | Paging unaffected (independent path); backlog processed on recovery; incidents older than 15 min become reports **[design]** |
| Telemetry backends | degrade | Tool errors are returned to the agent; confidence drops; < 0.6 means report-only **[built]** |
| K8s API / Argo CD | **fail closed** | Proposal pending, escalate after 2 attempts **[design]** |
| Slack / PagerDuty approval path | **fail closed** | No tier-2 actions; never auto-approve **[built]** (timeout) |

**Consistency (CAP):**

| State | Consistency | Store | Under partition |
|---|---|---|---|
| Target leases, remediation budgets, approvals | **Linearizable** | Postgres row locks / etcd (SQLite in PoC) | **Deny**: choose consistency over availability |
| Run state and transitions | Strong per run, append-only | Postgres / Temporal history (SQLite in PoC) | Run parks, resumes after heal |
| Dedup and correlation windows | Eventually consistent | Stream processor state + changelog | Brief duplicates tolerated; incident IDs and leases absorb them |
| Metrics, traces, audit shipping | Eventually consistent | Prometheus, OTel collector, object store | Buffered; the audit record is committed locally *before* the action |

**Multi-region [design]:** one control plane per region acts only on its own clusters; a cross-region incident yields per-region reports and a human decision. This avoids a global lock at the cost of no automatic cross-region remediation.

---

## 6. Known limitations of the PoC

- Single process: identity separation is modelled with tokens, not separate deployments.
- Approval waits block the run (production parks them in a durable workflow).
- The simulator's verification "waits" by advancing a virtual clock.
- The deterministic reasoner is rule-based; diagnosis quality with a real LLM depends on the model. The guardrails do not.
- Rego runs through the `opa` binary (preferred) or `regopy`; both evaluate the same file, and both paths are tested.
