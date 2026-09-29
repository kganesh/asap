"""Run the simulator in a background thread (demo/tests) or standalone (`asap sim`)."""

from __future__ import annotations

import socket
import threading
import time

import httpx
import uvicorn

from .api import build_app
from .scenarios import SCENARIOS
from .world import World


class SimServer:
    def __init__(self, world: World, port: int | None = None, host: str = "127.0.0.1") -> None:
        self.world_ref = {"world": world}
        self.host = host
        self.port = port or _free_port()
        config = uvicorn.Config(build_app(self.world_ref), host=host, port=self.port, log_level="error",
                                access_log=False)
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def world(self) -> World:
        return self.world_ref["world"]

    def load(self, world: World) -> None:
        self.world_ref["world"] = world

    def start(self) -> SimServer:
        self._thread.start()
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                httpx.get(f"{self.url}/docs", timeout=0.5)
                return self
            except httpx.HTTPError:
                time.sleep(0.05)
        raise RuntimeError("simulator failed to start")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def serve_forever(scenario: str, host: str, port: int) -> None:
    uvicorn.run(build_app({"world": SCENARIOS[scenario].build()}), host=host, port=port, log_level="info")
