"""Wires a scenario end to end: simulator -> alert pipeline -> orchestrator. Used by the CLI and tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .agent.llm import LLM
from .agent.orchestrator import Orchestrator
from .agent.states import Limits
from .config import Settings
from .control.approval import ApprovalGate
from .control.plane import Controls
from .control.policy import PolicyEngine
from .control.store import StateStore
from .ingest.pipeline import AlertPipeline, Incident
from .models import RunState
from .sim.scenarios import SCENARIOS
from .sim.server import SimServer
from .simclient import SimClient


@dataclass
class Env:
    server: SimServer
    store: StateStore
    policy: PolicyEngine
    runs_dir: Path
    settings: Settings = field(default_factory=Settings)

    @classmethod
    def create(cls, runs_dir: Path, scenario: str = "bad_deploy", fresh_store: bool = True,
               settings: Settings | None = None) -> Env:
        settings = settings or Settings()
        runs_dir.mkdir(parents=True, exist_ok=True)
        db = runs_dir / "state.db"
        if fresh_store and db.exists():
            db.unlink()
        server = SimServer(SCENARIOS[scenario].build()).start()
        return cls(server, StateStore(db), PolicyEngine(settings=settings.control), runs_dir, settings)

    def load(self, scenario: str) -> None:
        self.server.load(SCENARIOS[scenario].build())

    def incidents(self) -> list[Incident]:
        sim = SimClient.reader(self.server.url)
        catalog = sim.catalog()
        deps = {s: sim.dependencies(s)["downstream"] for s in catalog}
        tiers = {s: v["tier"] for s, v in catalog.items()}
        pipeline = AlertPipeline(deps, tiers, self.settings.ingest,
                                 shared_cause_incidents=self.policy.constants.get("storm_incident_threshold"))
        return pipeline.process(sim.alerts(), sim.now())

    def orchestrator(self, llm: LLM, approval: ApprovalGate, ui: object | None = None,
                     controls: Controls | None = None, limits: Limits | None = None) -> Orchestrator:
        return Orchestrator(self.server.url, llm, self.store, self.policy, approval, self.runs_dir, ui,
                            controls, limits, self.settings)

    def close(self) -> None:
        self.server.stop()


def run_scenario(env: Env, scenario: str, llm: LLM, approval: ApprovalGate, ui: object | None = None,
                 controls: Controls | None = None, limits: Limits | None = None) -> tuple[RunState, Incident]:
    env.load(scenario)
    incidents = env.incidents()
    if not incidents:
        raise RuntimeError(f"scenario {scenario} produced no incidents")
    top = incidents[0]
    run = env.orchestrator(llm, approval, ui, controls, limits).run(top)
    return run, top
