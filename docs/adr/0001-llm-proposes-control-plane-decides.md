# ADR-0001: The LLM proposes; a deterministic control plane decides and acts

- **Status:** Accepted. Built in the PoC.
- **Date:** 2026-09-29

## Context

An SRE agent has to take actions (roll back, scale, restart, flush) to be useful. An LLM's output is non-deterministic, can be manipulated through the telemetry it reads (prompt injection in log lines), and can be confidently wrong. The failure the assignment calls out is "hallucinated remediation": dropping a database, or looping restarts.

## Options considered

| Option | For | Against |
|---|---|---|
| A. LLM calls production tools directly, constrained by the prompt | Simplest; the most "agentic" | Safety depends on model behavior; one injected string or bad inference can act on production. The rubric's explicit red flag |
| B. LLM calls tools through an allowlist proxy | Blocks unknown commands | The allowlist is per call, not per situation: a *valid* rollback of the wrong service, at the wrong time or with no evidence still goes through |
| **C. LLM emits a typed proposal; a separate control plane decides, and a separate executor acts** | Every safety property is deterministic and testable; the model can be swapped or broken without changing what is allowed | More components; the agent can't "just do it" even when it's obviously right |

## Decision

Option C. The agent has read tools plus four action tools that only create proposals. The control plane checks evidence and scope, fetches its own context, runs a dry-run, scores blast radius, evaluates Rego, and routes to approval. The executor is the only component with write credentials, and it re-validates, applies idempotently and verifies.

## Consequences

- Safety can be proven with hostile scripted models (`asap attack`, 15 attacks) rather than argued from prompt wording.
- The agent can't see or influence the policy input, so it can't argue its way into a better verdict.
- Extra latency per action (policy evaluation + dry-run + re-validation), well under a second, and negligible next to the verification window.
- Every new action type needs a schema, a dry-run, policy rules and a revert rule. That friction is deliberate.

## Revisit if

A class of remediation needs multi-step, conditional execution that doesn't fit a single typed proposal (for example, a coordinated failover). The answer would be a runbook-as-code action type approved as a unit, not direct tool access.
