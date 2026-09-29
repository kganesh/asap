"""Shared definitions of what counts as a signal. One place, so the agent-side tools and the control plane
can never disagree about what "anomalous" means.

These are algorithm constants, not deployment settings: changing them changes what the evidence says, so
they move with code review and the scenario suite, not with an environment variable.
"""

from __future__ import annotations

# A value is anomalous when it exceeds BOTH ANOMALY_RATIO x baseline and baseline + ANOMALY_MIN_DELTA.
# The absolute delta stops a near-zero baseline (0.001 -> 0.003 error ratio) from looking like an incident.
ANOMALY_RATIO = 2.0
ANOMALY_MIN_DELTA = 0.02

# A change point needs this many consecutive anomalous samples, so one noisy scrape is not a change.
CHANGE_POINT_CONSECUTIVE_SAMPLES = 3

# A deploy is a suspect only if the symptom began within this many minutes of the rollout.
SUSPECT_DEPLOY_WINDOW_MIN = 15

# A downstream dependency "dominates" latency when it accounts for at least this share of the root's p99.
DOMINANT_DOWNSTREAM_SHARE = 0.6


def anomaly_threshold(baseline: float) -> float:
    return max(baseline * ANOMALY_RATIO, baseline + ANOMALY_MIN_DELTA)


def is_anomalous(value: float, baseline: float) -> bool:
    return value > anomaly_threshold(baseline)
