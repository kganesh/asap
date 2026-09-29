"""Simulated production cluster.

The world is a small, deterministic model of a microservice estate: workloads with
revision history, HPAs, PDBs, a dependency graph, and fault conditions. Telemetry is
*computed* from world state at a given time, so remediations change what the telemetry
shows afterwards. Verification is therefore real, not scripted.

Time is virtual (epoch seconds) so a demo can "wait five minutes" instantly.
"""

from __future__ import annotations

import hashlib
import random
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

MIN = 60.0
NAMESPACE = "shop"
CLUSTER = "us-west-2-prod-1"
CELL = "cell-a"

# Alert rule thresholds (what Prometheus alerting rules would encode).
ERROR_RATIO_THRESHOLD = 0.05
LATENCY_P99_THRESHOLD_S = 1.0
THROTTLE_THRESHOLD = 0.25
ALERT_FOR_S = 120  # `for: 2m`


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _noise(*parts: object) -> float:
    """Deterministic noise in [-1, 1] keyed by its inputs."""
    h = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return (int.from_bytes(h[:4], "big") / 0xFFFFFFFF) * 2 - 1


@dataclass
class Revision:
    revision: int
    version: str
    image: str
    change_cause: str
    created: float
    schema_migration: bool = False


@dataclass
class Workload:
    name: str
    kind: str  # Deployment | StatefulSet
    tier: int  # 0 = most critical
    owners: list[str]
    deps: list[str]
    base_rps: float
    base_p99: float
    capacity_rps_per_replica: float
    replicas: int
    http: bool = True  # has HTTP SLO alert rules
    hpa_min: int | None = None
    hpa_max: int | None = None
    pdb_min_available: int | None = None
    gitops_managed: bool = True
    revisions: list[Revision] = field(default_factory=list)
    current_revision: int = 1
    resource_version: int = 1
    restarts: int = 0
    version_history: list[tuple[float, str]] = field(default_factory=list)
    replica_history: list[tuple[float, int]] = field(default_factory=list)

    @property
    def current(self) -> Revision:
        return next(r for r in self.revisions if r.revision == self.current_revision)


def _at(history: list[tuple[float, object]], t: float, default: object) -> object:
    val = default
    for ts, v in history:
        if ts <= t:
            val = v
        else:
            break
    return val


class World:
    def __init__(self, now: float, scenario: str = "custom") -> None:
        self.now = now
        self.scenario = scenario
        self.workloads: dict[str, Workload] = {}
        self.bad_versions: dict[str, set[str]] = {}
        self.traffic_events: dict[str, list[tuple[float, float]]] = {}
        self.db_slow: dict[str, list[tuple[float, float | None]]] = {}
        self.injected_log_lines: dict[str, list[tuple[float, str, str]]] = {}
        self.cache_flushes: list[tuple[float, str, str]] = []
        self.events: list[dict] = []  # write-path audit inside the simulator
        self.allocatable_pods = 80
        self.lock = threading.RLock()

    # ------------------------------------------------------------------ state lookups
    def add(self, w: Workload) -> None:
        if not w.version_history:
            w.version_history = [(r.created, r.version) for r in sorted(w.revisions, key=lambda r: r.created)]
        if not w.replica_history:
            w.replica_history = [(0.0, w.replicas)]
        self.workloads[w.name] = w

    def version_at(self, s: str, t: float) -> str:
        w = self.workloads[s]
        return str(_at(w.version_history, t, w.current.version))

    def replicas_at(self, s: str, t: float) -> int:
        w = self.workloads[s]
        return int(_at(w.replica_history, t, w.replicas))  # type: ignore[arg-type]

    def traffic_mult(self, s: str, t: float) -> float:
        return float(_at(self.traffic_events.get(s, []), t, 1.0))  # type: ignore[arg-type]

    def is_db_slow(self, s: str, t: float) -> bool:
        return any(start <= t and (end is None or t < end) for start, end in self.db_slow.get(s, []))

    # ------------------------------------------------------------------ metric model
    def rps(self, s: str, t: float) -> float:
        w = self.workloads[s]
        return w.base_rps * self.traffic_mult(s, t) * (1 + 0.03 * _noise(s, "rps", int(t // 15)))

    def throttle_ratio(self, s: str, t: float) -> float:
        w = self.workloads[s]
        util = self.rps(s, t) / (self.replicas_at(s, t) * w.capacity_rps_per_replica)
        return max(0.0, min(0.95, (util - 0.7) * 0.8))

    def error_ratio(self, s: str, t: float) -> float:
        w = self.workloads[s]
        own = 0.003 + 0.002 * _noise(s, "err", int(t // 15))
        if self.version_at(s, t) in self.bad_versions.get(s, set()):
            own += 0.18 * (1 + 0.04 * _noise(s, "bad", int(t // 15)))
        down = max((self.error_ratio(d, t) for d in w.deps if self.workloads[d].http), default=0.0)
        return max(0.0, min(1.0, own + 0.5 * down))

    def latency_p99(self, s: str, t: float) -> float:
        w = self.workloads[s]
        own = w.base_p99 * (1 + 0.05 * _noise(s, "lat", int(t // 15))) + 2.5 * self.throttle_ratio(s, t)
        if self.is_db_slow(s, t):
            own += 1.25 * (1 + 0.05 * _noise(s, "db", int(t // 15)))
        down = max((self.latency_p99(d, t) for d in w.deps), default=0.0)
        return own + 0.9 * down

    def memory_bytes(self, s: str, t: float) -> float:
        return 512e6 * (1 + 0.02 * _noise(s, "mem", int(t // 15)))

    METRICS = {
        "error_ratio": "error_ratio",
        "latency_p99_seconds": "latency_p99",
        "cpu_throttle_ratio": "throttle_ratio",
        "request_rate": "rps",
        "memory_working_set_bytes": "memory_bytes",
        "replicas_available": "replicas_at",
    }

    def series(self, metric: str, s: str, minutes: int, step_s: int = 30) -> list[tuple[float, float]]:
        fn = getattr(self, self.METRICS[metric])
        start = self.now - minutes * MIN
        pts, t = [], start
        while t <= self.now + 1e-6:
            pts.append((t, float(fn(s, t))))
            t += step_s
        return pts

    # ------------------------------------------------------------------ alerting
    def _alert_conditions(self, s: str, t: float) -> dict[str, tuple[bool, str, str]]:
        err, p99, thr = self.error_ratio(s, t), self.latency_p99(s, t), self.throttle_ratio(s, t)
        return {
            "HighErrorRate": (err > ERROR_RATIO_THRESHOLD, "critical", f"5xx ratio {err:.1%} > {ERROR_RATIO_THRESHOLD:.0%}"),
            "HighLatencyP99": (p99 > LATENCY_P99_THRESHOLD_S, "warning", f"p99 {p99:.2f}s > {LATENCY_P99_THRESHOLD_S:.1f}s"),
            "CPUThrottlingHigh": (thr > THROTTLE_THRESHOLD, "warning", f"CFS throttled {thr:.0%} > {THROTTLE_THRESHOLD:.0%}"),
        }

    def firing_alerts(self) -> list[dict]:
        """Alertmanager v2 `/api/v2/alerts` shaped payload for currently firing alerts."""
        out = []
        for s, w in self.workloads.items():
            if not w.http:
                continue
            now_c = self._alert_conditions(s, self.now)
            for name, (active, sev, desc) in now_c.items():
                if not active or not self._alert_conditions(s, self.now - ALERT_FOR_S)[name][0]:
                    continue
                t = self.now
                while t - 30 >= self.now - 60 * MIN and self._alert_conditions(s, t - 30)[name][0]:
                    t -= 30
                labels = {"alertname": name, "service": s, "namespace": NAMESPACE, "cluster": CLUSTER,
                          "cell": CELL, "severity": sev}
                fp = hashlib.sha1(repr(sorted(labels.items())).encode()).hexdigest()[:16]
                out.append({
                    "labels": labels,
                    "annotations": {"summary": f"{name} on {s}", "description": desc,
                                    "runbook_url": f"https://runbooks.internal/{name}"},
                    "startsAt": iso(t),
                    "endsAt": iso(self.now + 4 * MIN),
                    "status": {"state": "active"},
                    "fingerprint": fp,
                    "generatorURL": f"http://prometheus.{CLUSTER}/graph?g0.expr={name}",
                })
        return out

    # ------------------------------------------------------------------ logs
    def log_templates(self, s: str, t: float) -> list[tuple[str, str, int, dict]]:
        """(level, template, lines_per_minute, attrs) active for service s at time t."""
        w = self.workloads[s]
        ver = self.version_at(s, t)
        out: list[tuple[str, str, int, dict]] = [("INFO", f"handled request path=/{s}/api status=200", 600, {"version": ver})]
        if ver in self.bad_versions.get(s, set()):
            out.append(("ERROR", "java.lang.NullPointerException: promoCode is null at "
                        "com.shop.checkout.PromoCodeResolver.apply(PromoCodeResolver.java:88)", 118,
                        {"version": ver, "exception.type": "java.lang.NullPointerException"}))
            out.append(("ERROR", "POST /checkout/submit status=500 upstream_error=false", 121, {"version": ver}))
        for d in w.deps:
            if self.workloads[d].http and self.error_ratio(d, t) > ERROR_RATIO_THRESHOLD:
                out.append(("WARN", f"upstream call to {d} failed status=500", 40, {"peer.service": d}))
        thr = self.throttle_ratio(s, t)
        if thr > 0.1:
            out.append(("WARN", "request queue depth exceeds soft limit (depth=%d limit=64)" % int(64 + 400 * thr), 30,
                        {"version": ver}))
        if self.traffic_mult(s, t) > 1.5:
            out.append(("INFO", "settlement batch fan-out active (partner=acme-retail)", 2, {}))
        if any(self.is_db_slow(d, t) for d in w.deps):
            out.append(("WARN", "slow query: SELECT qty FROM stock_levels WHERE sku=$1 FOR UPDATE took 1180ms", 55,
                        {"db.system": "postgresql", "version": ver}))
        if self.is_db_slow(s, t):
            out.append(("LOG", "process 4121 still waiting for ShareLock on transaction 99812 after 1000.041 ms", 48,
                        {"relation": "stock_levels"}))
            out.append(("LOG", "automatic vacuum of table \"shop.public.stock_levels\": index scans: 1", 1, {}))
        for ts, level, line in self.injected_log_lines.get(s, []):
            if ts <= t < ts + 10 * MIN:
                out.append((level, line, 1, {}))
        return out

    def search_logs(self, s: str, minutes: int, level: str | None, pattern: str | None, limit: int) -> dict:
        clusters: dict[tuple[str, str], dict] = {}
        start = self.now - minutes * MIN
        t = start
        while t <= self.now:
            for lvl, tmpl, rate, attrs in self.log_templates(s, t):
                if level and lvl != level.upper():
                    continue
                if pattern and pattern.lower() not in tmpl.lower():
                    continue
                c = clusters.setdefault((lvl, tmpl), {"level": lvl, "template": tmpl, "count": 0,
                                                       "first_seen": iso(t), "last_seen": iso(t), "exemplars": []})
                c["count"] += rate
                c["last_seen"] = iso(t)
                if len(c["exemplars"]) < 2:
                    rng = random.Random(f"{s}{tmpl}{t}")
                    c["exemplars"].append({"ts": iso(t + rng.random() * 50), "level": lvl, "service": s,
                                           "trace_id": "%032x" % rng.getrandbits(128), "msg": tmpl, **attrs})
            t += MIN
        ranked = sorted(clusters.values(), key=lambda c: -c["count"])
        return {"service": s, "window_minutes": minutes, "clusters": ranked[:limit], "total_clusters": len(ranked)}

    # ------------------------------------------------------------------ traces
    def trace_summary(self, s: str, minutes: int, limit: int) -> dict:
        w = self.workloads[s]
        root_p99 = self.latency_p99(s, self.now)
        rng = random.Random(f"traces{s}{self.now}")
        children = []
        for d in w.deps:
            dw = self.workloads[d]
            d_p99 = self.latency_p99(d, self.now)
            children.append({
                "peer.service": d,
                "span.kind": "CLIENT",
                "db.system": "postgresql" if dw.kind == "StatefulSet" and "postgres" in d else
                             ("redis" if "redis" in d else None),
                "p50_ms": round(d_p99 * 350, 1),
                "p99_ms": round(d_p99 * 1000, 1),
                "share_of_root_p99": round(min(1.0, 0.9 * d_p99 / root_p99), 2),
                "error_ratio": round(self.error_ratio(d, self.now) if dw.http else 0.0, 4),
            })
        children.sort(key=lambda c: -c["share_of_root_p99"])
        exemplars = []
        for _ in range(min(limit, 3)):
            span = {"trace_id": "%032x" % rng.getrandbits(128), "span_id": "%016x" % rng.getrandbits(64),
                    "service.name": s, "name": f"HTTP POST /{s}", "duration_ms": round(root_p99 * 1000 * (0.8 + 0.2 * rng.random()), 1),
                    "http.status_code": 500 if self.error_ratio(s, self.now) > 0.05 and rng.random() < 0.6 else 200,
                    "service.version": self.version_at(s, self.now)}
            if children:
                top = children[0]
                span["slowest_child"] = {"peer.service": top["peer.service"], "duration_ms": top["p99_ms"],
                                         "db.system": top["db.system"]}
                if top["db.system"] == "postgresql":
                    span["slowest_child"]["db.statement"] = "SELECT qty FROM stock_levels WHERE sku=$1 FOR UPDATE"
            if span["http.status_code"] == 500 and self.version_at(s, self.now) in self.bad_versions.get(s, set()):
                span["exception.type"] = "java.lang.NullPointerException"
            exemplars.append(span)
        return {"service": s, "window_minutes": minutes, "root_p99_ms": round(root_p99 * 1000, 1),
                "downstream": children, "exemplar_spans": exemplars}

    # ------------------------------------------------------------------ k8s-ish views
    def deployment_manifest(self, s: str) -> dict:
        w = self.workloads[s]
        rev = w.current
        return {
            "apiVersion": "apps/v1", "kind": w.kind,
            "metadata": {"name": s, "namespace": NAMESPACE, "resourceVersion": str(w.resource_version),
                         "annotations": {"deployment.kubernetes.io/revision": str(rev.revision),
                                         "argocd.argoproj.io/managed": str(w.gitops_managed).lower()},
                         "labels": {"app": s, "tier": f"tier-{w.tier}"}},
            "spec": {"replicas": self.replicas_at(s, self.now),
                     "template": {"spec": {"containers": [{"name": s, "image": rev.image,
                                                           "resources": {"limits": {"cpu": "1000m", "memory": "1Gi"}}}]}}},
            "status": {"availableReplicas": self.replicas_at(s, self.now), "observedGeneration": w.resource_version},
        }

    def history(self, s: str) -> list[dict]:
        w = self.workloads[s]
        return [{"revision": r.revision, "version": r.version, "image": r.image, "change_cause": r.change_cause,
                 "created": iso(r.created), "age_minutes": round((self.now - r.created) / MIN, 1),
                 "annotations": {"asap.io/schema-migration": str(r.schema_migration).lower()},
                 "current": r.revision == w.current_revision}
                for r in sorted(w.revisions, key=lambda r: -r.revision)]

    def resource_state(self, s: str) -> dict:
        w = self.workloads[s]
        used = sum(self.replicas_at(x, self.now) for x in self.workloads)
        return {"name": s, "kind": w.kind, "namespace": NAMESPACE, "tier": w.tier, "owners": w.owners,
                "replicas": self.replicas_at(s, self.now), "restarts_last_hour": w.restarts,
                "hpa": None if w.hpa_min is None else {"minReplicas": w.hpa_min, "maxReplicas": w.hpa_max},
                "pdb": None if w.pdb_min_available is None else {"minAvailable": w.pdb_min_available},
                "resourceVersion": str(w.resource_version), "gitops_managed": w.gitops_managed,
                "current_revision": w.current_revision, "current_version": w.current.version,
                "cluster_pods_used": used, "cluster_pods_allocatable": self.allocatable_pods}

    def dependencies(self, s: str) -> dict:
        upstream = sorted(x for x, w in self.workloads.items() if s in w.deps)
        return {"service": s, "downstream": list(self.workloads[s].deps), "upstream": upstream}

    # ------------------------------------------------------------------ write path (executor only)
    def _event(self, kind: str, **kw: object) -> dict:
        ev = {"ts": iso(self.now), "kind": kind, **kw}
        self.events.append(ev)
        return ev

    def gitops_revert(self, s: str, to_revision: int) -> dict:
        w = self.workloads[s]
        target = next((r for r in w.revisions if r.revision == to_revision), None)
        if target is None:
            raise ValueError(f"revision {to_revision} does not exist for {s}")
        new = Revision(revision=max(r.revision for r in w.revisions) + 1, version=target.version, image=target.image,
                       change_cause=f"git revert: roll back to revision {to_revision} (asap)", created=self.now + 30,
                       schema_migration=False)
        w.revisions.append(new)
        w.current_revision = new.revision
        w.version_history.append((self.now + 30, target.version))  # Argo CD sync lands ~30s later
        w.resource_version += 1
        return self._event("gitops_revert", target=s, to_revision=to_revision, new_revision=new.revision,
                           version=target.version)

    def set_hpa_min(self, s: str, min_replicas: int) -> dict:
        w = self.workloads[s]
        if w.hpa_min is None:
            raise ValueError(f"{s} has no HPA")
        if min_replicas > (w.hpa_max or 0):
            raise ValueError(f"minReplicas {min_replicas} exceeds HPA max {w.hpa_max}")
        prev = w.hpa_min
        w.hpa_min = min_replicas
        new = max(self.replicas_at(s, self.now), min_replicas)
        w.replicas = new
        w.replica_history.append((self.now + 20, new))  # pods ready ~20s later
        w.resource_version += 1
        return self._event("hpa_min_changed", target=s, previous=prev, min_replicas=min_replicas, replicas=new)

    def rollout_restart(self, s: str) -> dict:
        w = self.workloads[s]
        if w.kind != "Deployment":
            raise ValueError("rollout restart is only supported for Deployments")
        w.restarts += 1
        w.resource_version += 1
        return self._event("rollout_restart", target=s)

    def cache_flush(self, s: str, prefix: str) -> dict:
        self.cache_flushes.append((self.now, s, prefix))
        return self._event("cache_flush", target=s, prefix=prefix)

    def advance(self, minutes: float) -> float:
        self.now += minutes * MIN
        return self.now
