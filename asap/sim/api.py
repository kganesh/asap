"""HTTP facade over the World, shaped like the backends an SRE agent would really call.

Read endpoints (Prometheus-, Loki-, Tempo-, K8s-, Alertmanager-like) accept the reader or the
executor token. Write endpoints accept ONLY the executor token: the agent's credential cannot
change the cluster even if the agent tried to call these URLs directly.
"""

from __future__ import annotations

import os

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from .world import NAMESPACE, World, iso

READER_TOKEN = os.environ.get("ASAP_SIM_READER_TOKEN", "asap-reader-token")
EXECUTOR_TOKEN = os.environ.get("ASAP_SIM_EXECUTOR_TOKEN", "asap-executor-token")


class RevertBody(BaseModel):
    deployment: str
    to_revision: int


class HpaBody(BaseModel):
    min_replicas: int


class FlushBody(BaseModel):
    cache: str
    key_prefix: str


class AdvanceBody(BaseModel):
    minutes: float


def build_app(world_ref: dict) -> FastAPI:
    """world_ref = {"world": World}; indirection lets the harness swap scenarios."""
    app = FastAPI(title="ASAP cluster simulator", version="1.0")

    def world() -> World:
        return world_ref["world"]

    def reader(authorization: str = Header(default="")) -> None:
        if authorization not in (f"Bearer {READER_TOKEN}", f"Bearer {EXECUTOR_TOKEN}"):
            raise HTTPException(401, "missing or invalid token")

    def executor(authorization: str = Header(default="")) -> None:
        if authorization != f"Bearer {EXECUTOR_TOKEN}":
            raise HTTPException(403, "write verbs require the executor credential")

    def svc(w: World, name: str) -> None:
        if name not in w.workloads:
            raise HTTPException(404, f"unknown service {name}")

    @app.get("/api/time")
    def time_(_: None = Depends(reader)) -> dict:
        w = world()
        return {"now": w.now, "iso": iso(w.now), "scenario": w.scenario}

    @app.get("/api/v1/query_range")
    def query_range(metric: str, service: str, minutes: int = 30, step: int = 30, _: None = Depends(reader)) -> dict:
        w = world()
        svc(w, service)
        if metric not in World.METRICS:
            raise HTTPException(400, f"unsupported metric {metric}")
        with w.lock:
            pts = w.series(metric, service, minutes, step)
        return {"status": "success", "data": {"resultType": "matrix", "result": [
            {"metric": {"__name__": metric, "service": service, "namespace": NAMESPACE},
             "values": [[t, f"{v:.6g}"] for t, v in pts]}]}}

    @app.get("/loki/api/v1/query")
    def logs(service: str, minutes: int = 15, level: str | None = None, pattern: str | None = None,
             limit: int = 10, _: None = Depends(reader)) -> dict:
        w = world()
        svc(w, service)
        with w.lock:
            return w.search_logs(service, minutes, level, pattern, limit)

    @app.get("/api/traces")
    def traces(service: str, minutes: int = 15, limit: int = 3, _: None = Depends(reader)) -> dict:
        w = world()
        svc(w, service)
        with w.lock:
            return w.trace_summary(service, minutes, limit)

    @app.get("/apis/apps/v1/namespaces/shop/workloads/{name}")
    def manifest(name: str, _: None = Depends(reader)) -> dict:
        w = world()
        svc(w, name)
        with w.lock:
            return w.deployment_manifest(name)

    @app.get("/apis/apps/v1/namespaces/shop/workloads/{name}/history")
    def history(name: str, _: None = Depends(reader)) -> dict:
        w = world()
        svc(w, name)
        with w.lock:
            return {"name": name, "revisions": w.history(name)}

    @app.get("/apis/apps/v1/namespaces/shop/workloads/{name}/state")
    def state(name: str, _: None = Depends(reader)) -> dict:
        w = world()
        svc(w, name)
        with w.lock:
            return w.resource_state(name)

    @app.get("/api/dependencies/{name}")
    def deps(name: str, _: None = Depends(reader)) -> dict:
        w = world()
        svc(w, name)
        return w.dependencies(name)

    @app.get("/api/catalog")
    def catalog(_: None = Depends(reader)) -> dict:
        w = world()
        return {n: {"kind": x.kind, "role": x.role, "tier": x.tier, "owners": x.owners, "http": x.http}
                for n, x in w.workloads.items()}

    @app.get("/api/v2/alerts")
    def alerts(_: None = Depends(reader)) -> list[dict]:
        w = world()
        with w.lock:
            return w.firing_alerts()

    # ------------------------------------------------------------------ write path
    def _apply(fn, *args):  # type: ignore[no-untyped-def]
        w = world()
        try:
            with w.lock:
                return fn(*args)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

    @app.post("/admin/gitops/revert")
    def gitops_revert(body: RevertBody, _: None = Depends(executor)) -> dict:
        return _apply(world().gitops_revert, body.deployment, body.to_revision)

    @app.post("/admin/hpa/{name}")
    def hpa(name: str, body: HpaBody, _: None = Depends(executor)) -> dict:
        return _apply(world().set_hpa_min, name, body.min_replicas)

    @app.post("/admin/rollout-restart/{name}")
    def restart(name: str, _: None = Depends(executor)) -> dict:
        return _apply(world().rollout_restart, name)

    @app.post("/admin/cache/flush")
    def flush(body: FlushBody, _: None = Depends(executor)) -> dict:
        return _apply(world().cache_flush, body.cache, body.key_prefix)

    @app.post("/admin/clock/advance")
    def advance(body: AdvanceBody, _: None = Depends(executor)) -> dict:
        """Simulator-only: stands in for real wall-clock waiting during verification."""
        w = world()
        with w.lock:
            return {"now": w.advance(body.minutes), "iso": iso(w.now)}

    return app
