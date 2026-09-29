# ADR-0004: Remediation policy in OPA/Rego, evaluated fail-closed

- **Status:** Accepted. Built in the PoC.
- **Date:** 2026-09-29

## Context

Which actions may run unattended, need approval, or are never allowed is an organizational decision that changes over time (freeze windows, service tiers, new action types). It must be reviewable by people who don't read the orchestrator code, testable on its own, and impossible for the agent to influence.

## Options considered

| Option | For | Against |
|---|---|---|
| Rules hard-coded in Python | No extra dependency | Policy changes need a code release; policy is mixed with plumbing; hard for security or SRE leads to review |
| Rules in YAML plus a small in-house evaluator | Readable | A home-grown policy language with its own bugs and no ecosystem |
| **OPA/Rego** | Industry-standard policy-as-code; default-deny semantics; `opa test` unit tests; signed bundles and decision logs in production | A new language for some readers; an evaluator dependency |

## Decision

`policies/remediation.rego` is the single source of truth, with default deny, three explicit sets (`deny`, `require_approval`, and the resulting `decision`), and unit tests in `remediation_test.rego`. The control plane builds the input from data it fetches itself.

Evaluation uses the `opa` binary (downloaded by `make setup`), or `regopy` (rego-cpp bindings) when the binary isn't available. **Any evaluator failure, timeout or absence is treated as deny.**

## Consequences

- Both evaluators run the same file, and both paths are tested. Portability found one real issue: rego-cpp misparsed a line starting with `(` inside a rule body, so the headroom check was rewritten with a named variable and integer arithmetic.
- With no evaluator installed, ASAP still runs but denies every action. That is the intended behavior, and `asap doctor` warns about it.
- The Python blast-radius score is passed into Rego as an input rather than computed in Rego, which keeps the policy focused on thresholds.

## Revisit if

Policy must be shared across many services: move to an OPA sidecar with signed bundles and decision logs shipped to the audit store.
