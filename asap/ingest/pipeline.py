"""Alert ingestion funnel: debounce -> flap suppression -> fingerprint dedup -> group -> correlate.

Everything here is deterministic and runs before a single LLM token is spent. In production
this is a stream processor (Kafka Streams / Flink) with changelog-backed state, keyed by failure
domain (cluster + cell) so a cascade lands on one partition. In the PoC it is in-process.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

SEVERITY_WEIGHT = {"critical": 3, "warning": 2, "info": 1}
DEDUP_WINDOW_S = 300
CORRELATION_WINDOW_S = 120 * 5  # alerts within 10 minutes of each other in one cell may be one incident
FLAP_LIMIT = 2  # state flips in 10 minutes
DEBOUNCE_S = 120  # matches the rule's `for: 2m`
# If this many separate incidents open in one failure domain at once, the dependency graph probably doesn't
# contain the real cause (DNS, mesh, node pool, zone, provider): flag them so the agent and the policy know.
SHARED_CAUSE_INCIDENTS = 4


def _ts(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp() if "T" in s else float(s)


@dataclass
class Incident:
    incident_id: str
    cluster: str
    cell: str
    root_service: str
    services: list[str]
    alerts: list[dict]
    started_at: float
    priority: int
    severity: str
    suspected_shared_cause: bool = False  # many independent incidents in one failure domain at once

    @property
    def domain(self) -> str:
        return f"{self.cluster}/{self.cell}"

    @property
    def summary(self) -> str:
        names = sorted({f"{a['labels']['alertname']}({a['labels']['service']})" for a in self.alerts})
        return ", ".join(names)


@dataclass
class FunnelStats:
    received: int = 0
    resolved_or_expired: int = 0
    debounced: int = 0
    flapping: int = 0
    duplicates: int = 0
    unique: int = 0
    groups: int = 0
    incidents: int = 0
    notes: list[str] = field(default_factory=list)


class AlertPipeline:
    def __init__(self, dependencies: dict[str, list[str]], tiers: dict[str, int]) -> None:
        self.deps = dependencies  # service -> downstream services
        self.tiers = tiers
        self.seen: dict[str, float] = {}  # fingerprint -> last seen (dedup window state)
        self.transitions: dict[str, list[float]] = defaultdict(list)  # fingerprint -> state changes
        self.stats = FunnelStats()

    # ------------------------------------------------------------------ stage 1-3
    def _admit(self, alert: dict, now: float) -> bool:
        st = self.stats
        st.received += 1
        fp = alert.get("fingerprint") or _fingerprint(alert["labels"])
        state = (alert.get("status") or {}).get("state", "active")
        if state != "active":
            st.resolved_or_expired += 1
            return False
        recent = [t for t in self.transitions[fp] if now - t < 600]
        if len(recent) >= FLAP_LIMIT:
            st.flapping += 1
            return False
        if now - _ts(alert["startsAt"]) < DEBOUNCE_S:
            st.debounced += 1
            return False
        last = self.seen.get(fp)
        self.seen[fp] = now
        if last is not None and now - last < DEDUP_WINDOW_S:
            st.duplicates += 1
            return False
        st.unique += 1
        return True

    # ------------------------------------------------------------------ stage 4-5
    def _record_transitions(self, alerts: list[dict], now: float) -> None:
        """Count active<->resolved flips per fingerprint within this evaluation batch."""
        last_state: dict[str, str] = {}
        for a in alerts:
            fp = a.get("fingerprint") or _fingerprint(a["labels"])
            state = (a.get("status") or {}).get("state", "active")
            if fp in last_state and last_state[fp] != state:
                self.transitions[fp].append(now)
            last_state[fp] = state

    def process(self, alerts: list[dict], now: float) -> list[Incident]:
        self._record_transitions(alerts, now)
        admitted = [a for a in alerts if self._admit(a, now)]
        # group_by: (cluster, cell, service, alertname) collapses per-pod noise
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for a in admitted:
            lb = a["labels"]
            groups[(lb.get("cluster"), lb.get("cell"), lb["service"], lb["alertname"])].append(a)
        self.stats.groups += len(groups)
        # correlate per failure domain using the dependency graph
        by_domain: dict[tuple, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
        for (cluster, cell, service, _), items in groups.items():
            by_domain[(cluster, cell)][service].extend(items)
        incidents = []
        for (cluster, cell), svc_alerts in by_domain.items():
            domain_incidents = [self._incident(cluster, cell, component, svc_alerts)
                                for component in self._components(set(svc_alerts))]
            if len(domain_incidents) >= SHARED_CAUSE_INCIDENTS:
                self.stats.notes.append(f"{cluster}/{cell}: {len(domain_incidents)} simultaneous incidents; "
                                        "suspected shared-infrastructure cause")
                for i in domain_incidents:
                    i.suspected_shared_cause = True
            incidents.extend(domain_incidents)
        self.stats.incidents += len(incidents)
        return sorted(incidents, key=lambda i: (-i.priority, i.started_at))

    def _reachable(self, s: str) -> set[str]:
        out, stack = set(), [s]
        while stack:
            for d in self.deps.get(stack.pop(), []):
                if d not in out:
                    out.add(d)
                    stack.append(d)
        return out

    def _components(self, alerting: set[str]) -> list[set[str]]:
        """Connected components of alerting services over the (undirected) dependency graph."""
        comps, left = [], set(alerting)
        while left:
            seed = left.pop()
            comp, frontier = {seed}, [seed]
            while frontier:
                s = frontier.pop()
                for o in list(left):
                    if o in self._reachable(s) or s in self._reachable(o):
                        left.discard(o)
                        comp.add(o)
                        frontier.append(o)
            comps.append(comp)
        return comps

    def _incident(self, cluster: str, cell: str, comp: set[str], svc_alerts: dict[str, list[dict]]) -> Incident:
        # Root = the deepest alerting service: none of its downstream dependencies are alerting.
        roots = [s for s in comp if not (self._reachable(s) & comp)]
        root = sorted(roots, key=lambda s: (self.tiers.get(s, 9), s))[0]
        alerts = [a for s in sorted(comp) for a in svc_alerts[s]]
        started = min(_ts(a["startsAt"]) for a in alerts)
        sev = max((a["labels"].get("severity", "info") for a in alerts), key=lambda x: SEVERITY_WEIGHT.get(x, 0))
        window = int(started // CORRELATION_WINDOW_S)
        iid = "inc-" + hashlib.sha256(f"{cluster}|{cell}|{root}|{window}".encode()).hexdigest()[:10]
        priority = SEVERITY_WEIGHT.get(sev, 1) * 10 + (3 - min(self.tiers.get(root, 3), 3)) * 5 + len(comp)
        return Incident(iid, cluster, cell, root, sorted(comp), alerts, started, priority, sev)


def _fingerprint(labels: dict) -> str:
    return hashlib.sha1(repr(sorted(labels.items())).encode()).hexdigest()[:16]
