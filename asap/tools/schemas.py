"""Typed tool contracts. The JSON Schemas the LLM sees are generated from these models.

Three classes of tool, with different trust:
  READ      - executed directly by the gateway against telemetry backends (read-only credential)
  CONTROL   - agent workflow tools (plan, diagnosis); they only write run state
  ACTION    - never executed; they become proposals for the control plane

There is no free-form tool (no shell, kubectl, SQL). The action space is a closed enum.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TOOL_SCHEMA_VERSION = "2026-09-29.1"

MetricName = Literal["error_ratio", "latency_p99_seconds", "cpu_throttle_ratio", "request_rate",
                     "memory_working_set_bytes", "replicas_available"]
ServiceName = str


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ read tools
class QueryMetrics(_Strict):
    """Query a pre-approved PromQL template for one service. Returns a summary (baseline, current, peak, change point), not raw samples."""
    metric: MetricName
    service: ServiceName
    window_minutes: int = Field(30, ge=5, le=60)


class SearchLogs(_Strict):
    """Search a service's logs. Returns clustered log templates with counts and up to 2 exemplars each."""
    service: ServiceName
    level: Literal["ERROR", "WARN", "INFO", "LOG"] | None = None
    pattern: str | None = Field(None, max_length=80, description="case-insensitive substring filter")
    window_minutes: int = Field(15, ge=1, le=60)
    limit: int = Field(8, ge=1, le=20)


class GetTraces(_Strict):
    """Summarise recent traces for a service: root p99 and each downstream dependency's share of it, plus exemplar spans."""
    service: ServiceName
    window_minutes: int = Field(15, ge=1, le=60)


class GetDeploymentHistory(_Strict):
    """List a workload's revisions (version, image, change cause, age, schema-migration annotation)."""
    service: ServiceName


class GetResourceState(_Strict):
    """Current replicas, HPA, PDB, kind, owners, tier and resourceVersion of a workload."""
    service: ServiceName


class GetServiceDependencies(_Strict):
    """Upstream and downstream services from the trace-derived dependency graph."""
    service: ServiceName


# ------------------------------------------------------------------ workflow tools
class SubmitPlan(_Strict):
    """Record the investigation plan: ranked hypotheses and the checks that would confirm or refute each. Calling it again during investigation is a replan (max 2)."""
    hypotheses: list[str] = Field(min_length=1, max_length=5)
    steps: list[str] = Field(min_length=1, max_length=10)


class SubmitDiagnosis(_Strict):
    """End the investigation with a root cause. evidence_ids must reference results returned earlier in this run."""
    root_cause: str = Field(max_length=500)
    root_service: ServiceName
    category: Literal["bad_deploy", "saturation", "dependency_failure", "config", "unknown"]
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(min_length=1, max_length=10)
    recommended_action: Literal["rollback", "scale", "restart", "cache_flush", "none"]


# ------------------------------------------------------------------ action tools (proposals only)
class _Proposal(_Strict):
    evidence_ids: list[str] = Field(min_length=1, max_length=10)
    rationale: str = Field(max_length=400)


class ProposeRollback(_Proposal):
    """Propose rolling a Deployment back to an earlier revision (executed as a GitOps revert). Returns a proposal ID and the control plane's verdict."""
    deployment: ServiceName
    to_revision: int = Field(ge=1)


class ProposeScale(_Proposal):
    """Propose scaling a Deployment by setting its HPA minReplicas. Returns a proposal ID and the control plane's verdict."""
    deployment: ServiceName
    replicas: int = Field(ge=1, le=100)


class ProposeRestart(_Proposal):
    """Propose a rolling restart of a stateless Deployment."""
    deployment: ServiceName


class ProposeCacheFlush(_Proposal):
    """Propose flushing cache keys under a prefix (never a full flush)."""
    cache: ServiceName
    key_prefix: str = Field(min_length=3, max_length=64)


class NoAction(_Strict):
    """Recommend no automated action; the incident report goes to humans."""
    reason: str = Field(max_length=400)


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


def tool_specs(names: list[str]) -> list[dict]:
    """Provider-neutral tool specs: name, description, JSON schema."""
    specs = []
    for n in names:
        model = ALL_TOOLS[n]
        schema = model.model_json_schema()
        schema.pop("title", None)
        specs.append({"name": n, "description": (model.__doc__ or "").strip(), "input_schema": schema})
    return specs
