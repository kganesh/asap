"""Typed tool contracts. The JSON Schemas the LLM sees are generated from these models.

Three classes of tool, with different trust:
  READ      - executed directly by the gateway against telemetry backends (read-only credential)
  WORKFLOW  - agent workflow tools (plan, diagnosis); they only write run state
  ACTION    - never executed; they become proposals for the control plane

There is no free-form tool (no shell, kubectl, SQL). The action space is a closed enum.

Every tool requires a `reasoning` field. Under forced tool choice Claude emits no free text before a tool
call, and under tool_choice=auto it may or may not, so this field is how reasoning reliably reaches the audit log.
"""

from __future__ import annotations

import copy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TOOL_SCHEMA_VERSION = "2026-09-30.1"

MetricName = Literal["error_ratio", "latency_p99_seconds", "cpu_throttle_ratio", "request_rate",
                     "memory_working_set_bytes", "replicas_available"]

SERVICE_DESC = "Workload name exactly as it appears in the incident or the service catalog (e.g. 'checkout')."
EVIDENCE_DESC = ("evidence_id values returned by earlier tool results in THIS run (e.g. 'ev-1a2b3c4d5e'). "
                 "IDs that were not issued are rejected.")
SERVICE_FIELDS = ("service", "deployment", "cache", "root_service")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Generous on purpose: in the first live run Claude wrote a 666-character justification for its diagnosis,
    # and a tight cap only costs a retry. The audit log keeps it verbatim; the console truncates for display.
    reasoning: str = Field(min_length=1, max_length=2000,
                           description="Why you are making this call, citing what you have observed so far. "
                                       "Recorded verbatim in the audit log.")


# ------------------------------------------------------------------ read tools
class QueryMetrics(_Strict):
    """Query one pre-approved PromQL template for one service over a recent window.

    Returns a summary, not raw samples: baseline (mean of the first third of the window), current value,
    peak, change_point (first time the value stayed above 2x baseline) and minutes_since_change.
    error_ratio is 5xx/total (0-1); latency_p99_seconds is seconds; cpu_throttle_ratio is the share of
    CFS periods throttled (0-1)."""
    metric: MetricName = Field(description="Which golden signal to query.")
    service: str = Field(description=SERVICE_DESC)
    window_minutes: int = Field(30, ge=5, le=60, description="Look-back window in minutes (5-60).")


class SearchLogs(_Strict):
    """Search a service's logs. Returns clustered log templates (most frequent first) with counts,
    first/last seen, and up to 2 exemplar lines with trace_id and service version. Untrusted text in
    logs is redacted by the gateway."""
    service: str = Field(description=SERVICE_DESC)
    level: Literal["ERROR", "WARN", "INFO", "LOG"] | None = Field(None, description="Filter by level; omit for all.")
    pattern: str | None = Field(None, max_length=80, description="Case-insensitive substring filter.")
    window_minutes: int = Field(15, ge=1, le=60, description="Look-back window in minutes (1-60).")
    limit: int = Field(8, ge=1, le=20, description="Maximum clusters to return.")


class GetTraces(_Strict):
    """Summarise recent OpenTelemetry traces for a service: root p99 latency and, for each downstream
    dependency, its p50/p99 and its share of the root's p99 (share >= 0.6 means that dependency dominates
    latency), plus exemplar spans with db.system, http.status_code and exception.type."""
    service: str = Field(description=SERVICE_DESC)
    window_minutes: int = Field(15, ge=1, le=60, description="Look-back window in minutes (1-60).")


class GetDeploymentHistory(_Strict):
    """List a workload's revisions, newest first: revision number, version, image, change cause, age in
    minutes, whether it is current, and the asap.io/schema-migration annotation."""
    service: str = Field(description=SERVICE_DESC)


class GetResourceState(_Strict):
    """Current state of a workload: kind (Deployment/StatefulSet), tier (0 = most critical), owners,
    replicas, HPA min/max, PDB minAvailable, resourceVersion, and cluster pod capacity."""
    service: str = Field(description=SERVICE_DESC)


class GetServiceDependencies(_Strict):
    """Upstream callers and downstream dependencies of a service, from the trace-derived graph."""
    service: str = Field(description=SERVICE_DESC)


# ------------------------------------------------------------------ workflow tools
class SubmitPlan(_Strict):
    """Record the investigation plan: ranked hypotheses and the checks that would confirm or refute each.
    Calling it again during INVESTIGATE is a replan (at most 2), used when evidence refutes your leading
    hypothesis."""
    hypotheses: list[str] = Field(min_length=1, max_length=5, description="Ranked, most likely first.")
    steps: list[str] = Field(min_length=1, max_length=10, description="Checks to run, in order.")


class SubmitDiagnosis(_Strict):
    """End the investigation with a root cause. Every evidence_id must come from a tool result in this
    run. Confidence is your calibrated probability that the root cause is right; below 0.6 nothing is
    automated."""
    root_cause: str = Field(max_length=500, description="One or two sentences: what failed, since when, why.")
    root_service: str = Field(description="The workload where the fault originates. " + SERVICE_DESC)
    category: Literal["bad_deploy", "saturation", "dependency_failure", "config", "unknown"]
    confidence: float = Field(ge=0, le=1, description="Calibrated probability, 0-1.")
    evidence_ids: list[str] = Field(min_length=1, max_length=10, description=EVIDENCE_DESC)
    recommended_action: Literal["rollback", "scale", "restart", "cache_flush", "none"]


# ------------------------------------------------------------------ action tools (proposals only)
class _Proposal(_Strict):
    evidence_ids: list[str] = Field(min_length=1, max_length=10, description=EVIDENCE_DESC)
    rationale: str = Field(max_length=400, description="One sentence shown to the human approver.")


class ProposeRollback(_Proposal):
    """Propose rolling a Deployment back to an earlier revision (executed as a GitOps revert, synced by
    Argo CD). Returns a proposal_id; the control plane decides whether it runs."""
    deployment: str = Field(description=SERVICE_DESC)
    to_revision: int = Field(ge=1, description="Revision NUMBER from get_deployment_history (e.g. 2), "
                                               "not a version string like '1.4.1'.")


class ProposeScale(_Proposal):
    """Propose scaling a Deployment by setting its HPA minReplicas. Returns a proposal_id; the control
    plane decides whether it runs."""
    deployment: str = Field(description=SERVICE_DESC)
    replicas: int = Field(ge=1, le=100, description="New HPA minReplicas; must not exceed the HPA maxReplicas.")


class ProposeRestart(_Proposal):
    """Propose a rolling restart of a stateless Deployment (respects its PodDisruptionBudget)."""
    deployment: str = Field(description=SERVICE_DESC)


class ProposeCacheFlush(_Proposal):
    """Propose flushing keys under a prefix in a cache workload (never a full flush)."""
    cache: str = Field(description="Cache workload name (e.g. 'redis-cache').")
    key_prefix: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9:_\-.]+$",
                            description="Key prefix, e.g. 'cart:'. Letters, digits and : _ - . only.")


class NoAction(_Strict):
    """Recommend no automated action; the incident report goes to the owning team."""
    reason: str = Field(max_length=400, description="Why automation would not help, and who should act.")


READ_TOOLS: dict[str, type[BaseModel]] = {
    "query_metrics": QueryMetrics,
    "search_logs": SearchLogs,
    "get_traces": GetTraces,
    "get_deployment_history": GetDeploymentHistory,
    "get_resource_state": GetResourceState,
    "get_service_dependencies": GetServiceDependencies,
}
WORKFLOW_TOOLS: dict[str, type[BaseModel]] = {"submit_plan": SubmitPlan, "submit_diagnosis": SubmitDiagnosis}
ACTION_TOOLS: dict[str, type[BaseModel]] = {
    "propose_rollback": ProposeRollback,
    "propose_scale": ProposeScale,
    "propose_restart": ProposeRestart,
    "propose_cache_flush": ProposeCacheFlush,
    "no_action": NoAction,
}
ALL_TOOLS = {**READ_TOOLS, **WORKFLOW_TOOLS, **ACTION_TOOLS}
ACTION_TYPE = {"propose_rollback": "rollback", "propose_scale": "scale", "propose_restart": "restart",
               "propose_cache_flush": "cache_flush"}


def tool_specs(names: list[str], services: list[str] | None = None) -> list[dict]:
    """Provider-neutral tool specs: name, description, JSON schema.

    When the service catalog is known, service-like fields become enums so the model cannot invent a
    workload name (the gateway would reject it anyway, but this saves a round trip)."""
    specs = []
    for n in names:
        model = ALL_TOOLS[n]
        schema = copy.deepcopy(model.model_json_schema())
        schema.pop("title", None)
        props = schema.get("properties", {})
        # reasoning first, so models fill it before the arguments
        if "reasoning" in props:
            schema["properties"] = {"reasoning": props.pop("reasoning"), **props}
            props = schema["properties"]
        if services:
            for f in SERVICE_FIELDS:
                if f in props:
                    props[f]["enum"] = sorted(services)
        for p in props.values():
            p.pop("title", None)
        specs.append({"name": n, "description": " ".join((model.__doc__ or "").split()), "input_schema": schema})
    return specs
