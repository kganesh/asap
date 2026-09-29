"""Thin HTTP client for the simulated backends. Each component gets its own client + credential."""

from __future__ import annotations

import httpx

from .sim.api import EXECUTOR_TOKEN, READER_TOKEN

DEFAULT_TIMEOUT_S = 5.0  # per request; a slow backend fails the read, and the caller decides (deny or skip)


class SimClient:
    def __init__(self, base_url: str, token: str, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._http = httpx.Client(base_url=base_url, timeout=timeout,
                                  headers={"Authorization": f"Bearer {token}"})

    @classmethod
    def reader(cls, base_url: str) -> SimClient:
        return cls(base_url, READER_TOKEN)

    @classmethod
    def executor(cls, base_url: str) -> SimClient:
        return cls(base_url, EXECUTOR_TOKEN)

    def get(self, path: str, **params: object) -> dict | list:
        r = self._http.get(path, params={k: v for k, v in params.items() if v is not None})
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: dict | None = None) -> dict:
        r = self._http.post(path, json=body or {})
        r.raise_for_status()
        return r.json()

    # convenience
    def now(self) -> float:
        return float(self.get("/api/time")["now"])  # type: ignore[index]

    def state(self, name: str) -> dict:
        return self.get(f"/apis/apps/v1/namespaces/shop/workloads/{name}/state")  # type: ignore[return-value]

    def history(self, name: str) -> list[dict]:
        return self.get(f"/apis/apps/v1/namespaces/shop/workloads/{name}/history")["revisions"]  # type: ignore[index]

    def catalog(self) -> dict:
        return self.get("/api/catalog")  # type: ignore[return-value]

    def alerts(self) -> list[dict]:
        return self.get("/api/v2/alerts")  # type: ignore[return-value]

    def dependencies(self, name: str) -> dict:
        return self.get(f"/api/dependencies/{name}")  # type: ignore[return-value]
