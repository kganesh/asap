# ADR-0007: Alert stream keyed by failure domain, not by service

- **Status:** Accepted. The funnel logic is built in process; Kafka and the stream processor are production design.
- **Date:** 2026-09-29

## Context

A cascading failure fires alerts on many services at once (`checkout` fails, and `frontend` and `cart` alert as victims). At 5,000 alerts per minute, each must be deduplicated and correlated into incidents *before* any LLM call. Correlation needs every related alert seen by the same consumer.

## Options considered

| Partition key | For | Against |
|---|---|---|
| Service | Even load; simple | A cascade across services lands on different partitions and consumers, so cross-service correlation needs a second fan-in stage |
| Alert fingerprint | Perfect dedup locality | Scatters everything; correlation impossible without a global stage |
| **Failure domain (cluster + cell)** | A cascade within a cell lands on one partition and one correlator; the dependency graph can root it at the deepest failing service | Hot partitions when a large cell fails; skew during exactly the storms that matter |

## Decision

Key `alerts.raw` by failure domain. Correlator state (dedup windows, open incidents) lives in changelog-backed stream-processor state (Flink or Kafka Streams), so a restart restores windows instead of re-opening the storm. Mitigate hot partitions by sub-keying on namespace when a partition's lag breaches its SLO.

## Consequences

- Measured in the PoC funnel (`asap storm`): 5,000 alerts → 120 resolved, 135 flapping, 2,495 debounced, 2,184 duplicates → 66 unique → 8 groups → **2 incidents**.
- Incident IDs are deterministic (hash of domain, root and window), so redelivered alerts map to the same incident, and the incident lease drops the duplicate run.
- Cross-cell incidents become separate incidents; a human links them. Accepted for now.

## Revisit if

Cross-cell dependencies become common (shared databases or queues across cells): add a second-stage correlator keyed by shared dependency.
