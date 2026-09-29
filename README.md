# ASAP: Autonomous SRE Agentic Platform (PoC)

ASAP receives a production alert, investigates telemetry with an LLM-driven agent, diagnoses the root cause, and then either executes a **safe, policy-approved remediation** or hands on-call an **evidence-backed incident report**.

The design rule behind everything: **the LLM only proposes, and a deterministic control plane decides and acts.** The agent gets read-only telemetry tools. Every write goes through a typed proposal, then OPA/Rego policy, a dry-run, a blast-radius check, human approval where required, an idempotent executor, and verification. Prompt text is never a safety control.

- [ARCHITECTURE.md](ARCHITECTURE.md): data plane and control plane, the agent state machine, tool contracts, and the choice of reasoning pattern
- [SYSTEM_DESIGN.md](SYSTEM_DESIGN.md): guardrails, blast radius and human-in-the-loop (HITL), audit, alert storms, measured LLM token cost and capacity, failure modes and consistency
- [docs/adr](docs/adr/README.md): ten Architecture Decision Records: options considered, decision, trade-offs accepted
- [docs/live-runs](docs/live-runs/README.md): evidence from live Claude Sonnet 5.5 runs of all three scenarios: reports and verifiable hash-chained audit logs

---

## Quickstart (about 2 minutes)

Requires macOS or Linux, `make`, and either [uv](https://docs.astral.sh/uv/) (recommended, which fetches Python for you) or Python 3.10+.

```bash
git clone https://github.com/kganesh/asap.git && cd asap
make setup        # creates .venv, installs ASAP, downloads the OPA policy engine into ./bin
make demo         # walks through the three failure scenarios, one at a time
```

`make demo` runs the scenarios one at a time. Before each one it shows what's broken and what to watch for, and waits for Enter; after each it shows a recap (outcome, action, policy verdict, report path). In the first scenario it stops at the approval gate and asks you to approve the rollback, standing in for Slack. Press `q` at any pause to stop.

To run a single scenario: `make demo-bad-deploy`, `make demo-cpu` or `make demo-db`. For a non-interactive run of all three: `make demo-auto`.

**No API key needed.** Without a key, ASAP uses a *deterministic reasoner*: a rule-based stand-in for the LLM that calls the same tools and goes through the same control plane and executor. To run the same workflow with a real model:

```bash
export ANTHROPIC_API_KEY=sk-ant-...           # Claude (default model: claude-sonnet-5-5, override with ASAP_MODEL)
make demo
# or a local model via Ollama (OpenAI-compatible API):
export OPENAI_BASE_URL=http://localhost:11434/v1 ASAP_MODEL=llama3.1
asap demo --llm openai
```

**Docker alternative** (no local Python):

```bash
make docker-demo          # = docker compose run --rm asap demo --approve auto --no-pause && ... attack
```

## What you will see

| Command | What it shows |
|---|---|
| `make demo` | Three incidents investigated end to end, one at a time with a pause and recap between them (table below) |
| `make attack` | 15 adversarial mock "LLMs" try to cause damage (drop the DB, restart-loop, prompt injection, fabricated or irrelevant evidence, and more). Every one is contained, and the table shows which guardrail stopped it |
| `make storm` | 5,000 alerts in one minute collapse to **2 incidents** (flap suppression, debounce, dedup, dependency-graph correlation) before any LLM token is spent |
| `make tokens` | Measures the input tokens each run would send to an LLM: typical runs, a worst case, early compaction vs prompt caching, and a tight per-run budget (see ADR-0010) |
| `make test` | Rego policy unit tests (`opa test`) plus 70 pytest tests covering the guardrails, production edge cases, token controls and code-review regressions |
| `.venv/bin/asap replay <run_id>` | Replays a run from its hash-chained audit log, with no LLM calls, and verifies the chain |

| Scenario | Injected fault | Correct outcome |
|---|---|---|
| `bad_deploy` | `checkout` v1.4.2 throws NullPointerExceptions; a log line also carries a **prompt injection** | Diagnose the deploy, then a **tier-2 rollback** via GitOps revert after **human approval**; verification shows recovery. The injection is redacted at the gateway |
| `cpu_throttle` | Traffic surge on `payments`; CFS throttling at 59% | **Tier-1 auto-remediation**: HPA `minReplicas` 4→8; throttling clears |
| `db_red_herring` | `inventory` latency from Postgres lock contention; a harmless config deploy happened 10 minutes earlier | Traces show the DB dominates, so the agent **replans**, rejects the deploy hypothesis, and ends **report-only** (stateful DB, page the DBAs). It must not roll back |

Every run writes to `runs/<run_id>/`: `report.md` (incident report), `audit.jsonl` (hash-chained reasoning trail), and `spans.jsonl` (OpenTelemetry spans). `runs/metrics.prom` holds Prometheus metrics about the agent itself.

## Commands

```
asap demo   [--scenario all|bad_deploy|cpu_throttle|db_red_herring] [--llm auto|scripted|anthropic|openai|ollama]
            [--approve prompt|auto|deny|timeout] [--no-pause] [-v]
asap attack [--only NAME ...] [-v]
asap storm  [--alerts 5000]
asap tokens                 # measured LLM input tokens per run: caching, compaction, budget
asap replay RUN_ID          asap verify-audit RUN_ID
asap doctor                 # which reasoner and policy engine will be used
asap sim --scenario NAME --port 8080    # run the simulator standalone; browse http://localhost:8080/docs
```

Environment: `ASAP_KILL_SWITCH=1` forces report-only; `ASAP_CHANGE_FREEZE=1` makes every action require approval; `ASAP_LOG_LEVEL=INFO` (or `--log-level`) shows operational logs on stderr. The audit trail is separate, in `runs/<run_id>/audit.jsonl`.

## Repository layout

```
asap/
  sim/          cluster simulator: workloads, revisions, HPA/PDB, faults; Prometheus/Loki/Tempo/K8s/Alertmanager-shaped HTTP API
  ingest/       alert funnel (flap, debounce, dedup, group, correlate) + 5,000-alert storm generator
  tools/        typed tool contracts (Pydantic -> JSON Schema), gateway (quotas, evidence IDs), telemetry sanitizer
  agent/        state machine, orchestrator, prompts, LLM adapters (Anthropic, OpenAI-compatible), deterministic
                reasoner, adversarial mock models, context manager (prompt caching, compaction, per-run token budget)
  control/      control plane: Rego evaluation (fail-closed), blast radius, budgets/leases/circuit breaker, approval gate
  executor/     the only component with write credentials: dry-run, precondition check, idempotent apply, verify, revert rules
  audit/        hash-chained audit log + anchors
  telemetry/    OpenTelemetry tracing, Prometheus metrics
policies/       remediation.rego (single source of truth) + remediation_test.rego
tests/          scenarios, adversarial attacks, production edge cases, unit tests
```

## What the PoC simplifies (and the production design)

| PoC | Production design (see SYSTEM_DESIGN.md) |
|---|---|
| One Python process | Separate deployments and identities for the orchestrator, control plane, and executor; NetworkPolicy blocks the agent from write APIs |
| Simulator write API with an executor-only token | Executor ServiceAccount scoped per namespace; GitOps repo token; Argo CD API |
| In-process alert funnel | Kafka keyed by failure domain + a stream processor with changelog-backed state |
| SQLite for leases, budgets, and run state | Linearizable store (Postgres row locks / etcd) + durable workflow engine (Temporal) so approval waits park instead of blocking |
| CLI approval prompt | Slack interactive message (signed) + PagerDuty note, ownership from the service catalog |
| Audit anchors in `runs/anchors.jsonl` | Chain heads anchored to WORM storage (S3 Object Lock) |
| Verification "waits" by advancing a virtual clock | Real wall-clock verification window |
| Token figures in `make tokens` are estimates from the deterministic reasoner, calibrated against one live Claude run (see SYSTEM_DESIGN.md) | Provider-reported usage per call, already logged in `audit.jsonl` on live runs; capacity model re-derived from those |
| Demos and CI use the deterministic reasoner; one live Claude Sonnet 5.5 run per scenario, all three correct ([evidence](docs/live-runs/README.md)), but no scored evaluation set yet | Offline evaluation set of replayed incidents scoring diagnosis accuracy per model and prompt version, gating upgrades and any tier-1 autonomy |

## Troubleshooting

- `asap doctor` shows `policy none (fail-closed)`: OPA didn't download. Run `brew install opa` or `.venv/bin/pip install regopy`. Without a policy engine, ASAP still runs but denies every action (by design).
- Python older than 3.10: install uv (`curl -LsSf https://astral.sh/uv/install.sh | sh`) and re-run `make setup`.
- Windows: use WSL or `make docker-demo`.
