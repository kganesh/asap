"""Tool gateway: the only way the agent touches anything.

* validates every call against its schema (unknown tools and extra fields are rejected)
* runs READ tools with a read-only credential, enforcing quotas
* summarises and sanitises results, and stamps each with an evidence ID
* never executes ACTION tools itself (the orchestrator hands those to the control plane)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
from pydantic import ValidationError

from ..models import Evidence, RunState, new_id
from ..simclient import SimClient
from ..timeutil import iso
from .sanitize import sanitize
from .schemas import ALL_TOOLS, READ_TOOLS

PROMQL = {
    "error_ratio": 'sum(rate(http_requests_total{{service="{s}",code=~"5.."}}[5m])) / '
                   'sum(rate(http_requests_total{{service="{s}"}}[5m]))',
    "latency_p99_seconds": 'histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket'
                           '{{service="{s}"}}[5m])))',
    "cpu_throttle_ratio": 'sum(rate(container_cpu_cfs_throttled_periods_total{{container="{s}"}}[5m])) / '
                          'sum(rate(container_cpu_cfs_periods_total{{container="{s}"}}[5m]))',
    "request_rate": 'sum(rate(http_requests_total{{service="{s}"}}[5m]))',
    "memory_working_set_bytes": 'sum(container_memory_working_set_bytes{{container="{s}"}})',
    "replicas_available": 'kube_deployment_status_replicas_available{{deployment="{s}"}}',
}
log = logging.getLogger(__name__)

MAX_READ_CALLS_PER_RUN = 20
MAX_RESULT_CHARS = 6000


class ToolRejected(Exception):
    pass


@dataclass
class ToolResult:
    ok: bool
    content: dict
    evidence_id: str | None = None


def summarize_series(values: list[list]) -> dict:
    pts = [(float(t), float(v)) for t, v in values]
    if not pts:
        return {"samples": 0}
    third = max(1, len(pts) // 3)
    base = sum(v for _, v in pts[:third]) / third
    cur_t, cur = pts[-1]
    peak_t, peak = max(pts, key=lambda p: p[1])
    thresh = max(base * 2, base + 0.02)
    change = None
    for i in range(third, len(pts) - 2):
        if all(v > thresh for _, v in pts[i:i + 3]):
            change = pts[i][0]
            break
    return {
        "baseline": round(base, 4), "current": round(cur, 4), "peak": round(peak, 4), "peak_at": iso(peak_t),
        "change_point": iso(change) if change else None,
        "minutes_since_change": round((cur_t - change) / 60, 1) if change else None,
        "trend_last_5_points": [round(v, 4) for _, v in pts[-5:]], "samples": len(pts),
    }


class ToolGateway:
    def __init__(self, sim_url: str) -> None:
        self.sim = SimClient.reader(sim_url)  # read-only credential

    def validate(self, name: str, args: dict) -> object:
        model = ALL_TOOLS.get(name)
        if model is None:
            raise ToolRejected(f"unknown tool '{name}': the action space is closed; available tools are "
                               f"{sorted(ALL_TOOLS)}")
        try:
            return model.model_validate(args)
        except ValidationError as e:
            raise ToolRejected(f"invalid arguments for {name}: {e.errors(include_url=False)}") from e

    def is_valid(self, name: str, args: dict) -> bool:
        try:
            self.validate(name, args)
            return True
        except ToolRejected:
            return False

    def run_read(self, run: RunState, name: str, args: dict) -> ToolResult:
        if name not in READ_TOOLS:
            raise ToolRejected(f"{name} is not a read tool")
        if run.tool_calls >= MAX_READ_CALLS_PER_RUN:
            raise ToolRejected(f"read-tool quota ({MAX_READ_CALLS_PER_RUN}) exhausted for this run")
        parsed = self.validate(name, args)
        run.tool_calls += 1
        try:
            kind, raw = getattr(self, f"_{name}")(parsed)
        except httpx.HTTPStatusError as e:
            log.info("read tool %s rejected by backend: HTTP %s", name, e.response.status_code)
            raise ToolRejected(f"{name} failed: HTTP {e.response.status_code} {e.response.text[:200]}") from e
        except httpx.HTTPError as e:
            log.warning("telemetry backend unavailable for %s: %s", name, e)
            raise ToolRejected(f"{name} failed: telemetry backend unavailable ({type(e).__name__})") from e
        flags: list[str] = []
        clean = sanitize(raw, flags)
        run.flagged_untrusted.extend(flags)
        eid = new_id("ev")
        if flags:
            log.warning("redacted %d instruction-like string(s) from %s result", len(flags), name)
            clean["_gateway_note"] = f"{len(flags)} untrusted instruction-like string(s) redacted"
        clean = _truncate(clean)
        args = parsed.model_dump(exclude={"reasoning"})  # type: ignore[attr-defined]
        run.evidence[eid] = Evidence(eid, name, kind, args, clean, self.sim.now())
        return ToolResult(True, {"evidence_id": eid, **clean}, eid)

    # ------------------------------------------------------------------ implementations
    def _query_metrics(self, a) -> tuple[str, dict]:  # type: ignore[no-untyped-def]
        r = self.sim.get("/api/v1/query_range", metric=a.metric, service=a.service, minutes=a.window_minutes)
        values = r["data"]["result"][0]["values"]  # type: ignore[index]
        return "metric", {"metric": a.metric, "service": a.service, "promql": PROMQL[a.metric].format(s=a.service),
                          "window_minutes": a.window_minutes, **summarize_series(values)}

    def _search_logs(self, a) -> tuple[str, dict]:  # type: ignore[no-untyped-def]
        return "logs", self.sim.get("/loki/api/v1/query", service=a.service, level=a.level, pattern=a.pattern,
                                    minutes=a.window_minutes, limit=a.limit)  # type: ignore[return-value]

    def _get_traces(self, a) -> tuple[str, dict]:  # type: ignore[no-untyped-def]
        return "traces", self.sim.get("/api/traces", service=a.service, minutes=a.window_minutes)  # type: ignore[return-value]

    def _get_deployment_history(self, a) -> tuple[str, dict]:  # type: ignore[no-untyped-def]
        return "deploy", {"service": a.service, "revisions": self.sim.history(a.service)}

    def _get_resource_state(self, a) -> tuple[str, dict]:  # type: ignore[no-untyped-def]
        return "state", self.sim.state(a.service)

    def _get_service_dependencies(self, a) -> tuple[str, dict]:  # type: ignore[no-untyped-def]
        return "deps", self.sim.dependencies(a.service)


def _truncate(d: dict) -> dict:
    import json
    s = json.dumps(d, default=str)
    if len(s) <= MAX_RESULT_CHARS:
        return d
    if "clusters" in d:
        d = {**d, "clusters": d["clusters"][:4], "_truncated": True}
    return d
