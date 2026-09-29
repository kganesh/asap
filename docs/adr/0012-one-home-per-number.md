# ADR-0012: One home for every number

- **Status:** Accepted. Built in the PoC.
- **Date:** 2026-09-29

## Context

A review found about 45 values written inline in method bodies. Most were harmless, but some were one fact stated in two places, which can drift apart silently:

- The anomaly rule (above 2x baseline and baseline + 0.02) was coded in both the tool gateway and the control plane. If only one changes, the agent and the policy disagree on what "anomalous" means.
- The system prompt told the model "max 2" replans and "confidence below 0.6". The real values live in `Limits` and in the Rego policy. Change either one and the prompt misleads the model.
- The pipeline's shared-cause threshold (4 incidents) repeated the policy's storm threshold.
- The Rego budget rules were keyed `actions_last_30m` while Python chose the window. Changing the window would have made the key and the denial message wrong.
- The `asap tokens` table header still said `chars/4` after the estimator was calibrated to 2.5.

## Options considered

| Option | For | Against |
|---|---|---|
| Named module constants only | Smallest change | Can't change a timeout or window per environment without a code change; duplicates stay possible |
| **Typed settings + policy constants + named algorithm constants** | Each kind of number has one owner; environments can tune operations without a deploy; drift is tested | One more module; constructors take a settings argument |
| Everything in config (including simulator fixtures and display widths) | Uniform | Configuring fixture data is noise; reviewers would read it as over-engineering |

## Decision

Every number has exactly one home, chosen by who should be able to change it:

1. **Operational settings** live in `asap/config.py`: frozen dataclasses (`Limits`, `LLMSettings`, `ControlSettings`, `ExecutionSettings`, `IngestSettings`). They cover models, endpoints, timeouts, time windows, lease TTLs and run caps. Each field names its `ASAP_*` override. The environment is read once, by `Settings.from_env()` at the CLI entry point. Components receive their section through the constructor and default to the built-in values, so tests never depend on the environment. Secrets are not settings: the SDKs read them.
2. **Policy thresholds** live in `policies/remediation.rego` as named constants: confidence floor, per-target budgets, blast-radius approval line, restart headroom, fleet budget and storm threshold. Python reads them (`PolicyEngine.constants`) and never restates them. The prompt's confidence rule and the pipeline's shared-cause flag come from there. The windows the budgets apply to are settings, passed in the policy input and echoed in the denial message.
3. **Algorithm constants** are named next to their code: the anomaly rule in `asap/signals.py` (shared by the gateway and the control plane), blast-radius weights in `plane.py`, and the deterministic reasoner's heuristics. They change with code review and the scenario suite, not with an environment variable, because changing them changes what the evidence says.

Simulator scenarios, console display widths, HTTP status codes and ID formats stay literal. They are fixtures and formats, not tunables.

## Consequences

- `asap doctor` prints the policy thresholds and any environment overrides in effect. Each run's `run_started` audit record includes the limits, control and execution settings, and the policy constants, so a decision can be reproduced with the values it was made under.
- Tests enforce three things (`tests/test_config.py`):
  - the parsed policy constants equal what OPA or regopy evaluates;
  - the prompt renders its numbers from the limits and the policy;
  - no module outside `config.py` reads the environment, except for secrets and the OpenTelemetry standard variable.
- With default settings, behavior is unchanged: the same rendered prompt, incident IDs, decisions and outcomes. The policy input's budget keys were renamed (`actions_short_window`, `actions_long_window`, plus the window lengths). The policy tests were updated with them.

## Revisit if

Settings need to change at runtime without a restart (for example, a fleet budget tightened during an incident). Then `ControlSettings` moves behind the control-plane config service, like the kill switch in production, while the three-homes rule stays the same.
