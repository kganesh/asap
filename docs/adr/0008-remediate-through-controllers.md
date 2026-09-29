# ADR-0008: Roll back through GitOps and scale through the HPA

- **Status:** Accepted. Built in the PoC against a simulated Argo CD sync and HPA.
- **Date:** 2026-09-29

## Context

Kubernetes clusters are run by reconcilers. Argo CD or Flux continuously applies the desired state from Git, and the HPA owns a Deployment's `replicas`. A remediation that patches the live object directly is silently undone on the next sync, which also confuses verification (the fix "worked", then the symptom returns).

## Options considered

| Option | For | Against |
|---|---|---|
| Patch live objects (`kubectl rollout undo`, `kubectl scale`) | Fastest; no external dependency | The GitOps reconciler re-applies the bad version; the HPA overwrites `replicas`; drift between Git and the cluster |
| Pause GitOps sync, patch, then commit | Fast and eventually consistent with Git | More moving parts; easy to leave sync paused |
| **Change the desired state: revert commit in Git (synced by Argo CD); set HPA `minReplicas`** | Works with the controllers; Git history records the remediation; nothing to undo later | Slower (sync interval); depends on Git and Argo CD availability |

## Decision

Rollback is a Git revert of the offending revision, synced by Argo CD. Scaling raises HPA `minReplicas` (bounded by `maxReplicas` and cluster capacity). Restart is a rollout restart that respects the PodDisruptionBudget. Dry-run refuses out-of-band rollbacks on workloads that aren't GitOps-managed.

Revert rules are per action: scale → restore the previous `minReplicas`; restart → nothing to revert; cache flush → cannot be undone; **rollback → never auto-revert** (it would redeploy the bad version), escalate instead.

## Consequences

- Remediation shows up in Git history and code review, which auditors and developers already use.
- Adds the Git and Argo CD path to the executor's dependencies; if it's unavailable, execution fails closed (ADR-0005).
- A raised `minReplicas` must be lowered later by a human or a follow-up job, or the service stays over-provisioned. The incident report calls this out.

## Revisit if

An emergency path with a stricter SLA is needed (sub-minute rollback): use "pause sync, patch, commit" behind a tier-2 approval.
