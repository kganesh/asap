"""Synthetic alert storm: 5,000 alerts in one minute, to exercise the funnel and the capacity model."""

from __future__ import annotations

import hashlib
import random

from ..sim.world import iso
from .pipeline import AlertPipeline, FunnelStats

DEPS = {"frontend": ["cart", "checkout"], "cart": ["redis-cache"], "checkout": ["payments", "inventory", "redis-cache"],
        "payments": [], "inventory": ["postgres-inventory"], "postgres-inventory": [], "redis-cache": [],
        "search": ["search-index"], "search-index": []}
TIERS = {"frontend": 1, "cart": 2, "checkout": 1, "payments": 1, "inventory": 1, "search": 2, "search-index": 2}


def _alert(name: str, service: str, cell: str, pod: str, sev: str, starts: float, state: str = "active") -> dict:
    labels = {"alertname": name, "service": service, "namespace": "shop", "cluster": "us-west-2-prod-1",
              "cell": cell, "severity": sev, "pod": pod}
    return {"labels": labels, "startsAt": iso(starts), "status": {"state": state},
            "fingerprint": hashlib.sha1(repr(sorted(labels.items())).encode()).hexdigest()[:16]}


def generate(now: float, total: int = 5000, seed: int = 7) -> list[dict]:
    rng = random.Random(seed)
    alerts: list[dict] = []
    started = now - 9 * 60
    # Cascade in cell-a: checkout bad deploy propagating to frontend; every pod re-fires each 15s evaluation.
    for svc, pods in (("checkout", 6), ("frontend", 12)):
        for p in range(pods):
            for name in ("HighErrorRate", "SLOBurnRateFast", "HighLatencyP99"):
                for _ in range(4):
                    alerts.append(_alert(name, svc, "cell-a", f"{svc}-{p}", "critical", started))
    # Independent incident in cell-b: search-index degradation dragging search.
    for svc, pods in (("search-index", 4), ("search", 8)):
        for p in range(pods):
            for _ in range(4):
                alerts.append(_alert("HighLatencyP99", svc, "cell-b", f"{svc}-{p}", "warning", started))
    # Flapping disk alerts across the fleet (fire/resolve churn).
    for i in range(60):
        for k in range(5):
            alerts.append(_alert("NodeDiskPressure", "node-exporter", "cell-c", f"node-{i}", "warning",
                                 now - 30, "active" if k % 2 == 0 else "resolved"))
    # Everything else: sub-2-minute blips (debounced) and exact repeats (deduplicated).
    while len(alerts) < total:
        svc = rng.choice(["cart", "payments", "inventory", "search"])
        if rng.random() < 0.55:
            alerts.append(_alert("PodCPUSpike", svc, rng.choice(["cell-a", "cell-b"]), f"{svc}-{rng.randint(0, 9)}",
                                 "info", now - rng.randint(5, 100)))
        else:
            alerts.append(rng.choice(alerts[:200]))
    rng.shuffle(alerts)
    return alerts[:total]


def run_storm(now: float, total: int = 5000) -> tuple[FunnelStats, list]:
    pipe = AlertPipeline(DEPS, TIERS)
    alerts = generate(now, total)
    # flapping alerts are processed in arrival order; resolved ones record transitions first
    incidents = pipe.process(alerts, now)
    return pipe.stats, incidents


def capacity_model(incidents_per_min: float, run_seconds: float = 90, tokens_in: int = 60_000,
                   tokens_out: int = 2_500, workers: int = 20) -> dict:
    """Defaults from the live Claude runs (38-61k input tokens processed, 2.1-2.5k output per run; see
    SYSTEM_DESIGN.md). Input tokens are processed tokens, most of them served from the prompt cache."""
    runs_per_worker_min = 60 / run_seconds
    needed = incidents_per_min / runs_per_worker_min
    return {
        "incidents_per_min": incidents_per_min,
        "runs_per_worker_per_min": round(runs_per_worker_min, 2),
        "workers_needed": round(needed, 1),
        "workers_provisioned": workers,
        "headroom": round(workers / max(needed, 1e-9), 1),
        "tokens_per_min": int(incidents_per_min * (tokens_in + tokens_out)),
    }
