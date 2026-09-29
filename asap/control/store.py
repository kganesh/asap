"""Durable control-plane state: remediation budget, circuit breaker, leases, idempotent execution steps,
and persisted run state.

SQLite in the PoC. Production needs a linearizable store (Postgres with row locks, or etcd):
budgets and leases are distributed locks, and under a partition the correct answer is "deny".
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
  proposal_id TEXT PRIMARY KEY, run_id TEXT, target TEXT, action TEXT, ts REAL, outcome TEXT);
CREATE TABLE IF NOT EXISTS leases (target TEXT PRIMARY KEY, holder TEXT, expires REAL);
CREATE TABLE IF NOT EXISTS exec_steps (
  proposal_id TEXT, step TEXT, status TEXT, detail TEXT, PRIMARY KEY (proposal_id, step));
CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, incident_id TEXT, state TEXT, updated REAL, data TEXT);
"""


class StateStore:
    def __init__(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.executescript(SCHEMA)
        self._lock = threading.Lock()
        self.unavailable = False  # test hook: simulate a lost store (fail closed)

    def _check(self) -> None:
        if self.unavailable:
            raise StoreUnavailable("control-plane state store unavailable")

    # ---------------------------------------------------------------- budget / breaker
    def budget(self, target: str, now: float) -> dict:
        self._check()
        c = self.db.execute
        last30 = c("SELECT COUNT(*) FROM actions WHERE target=? AND ts>?", (target, now - 1800)).fetchone()[0]
        last24 = c("SELECT COUNT(*) FROM actions WHERE target=? AND ts>?", (target, now - 86400)).fetchone()[0]
        return {"actions_last_30m": last30, "actions_last_24h": last24}

    def circuit_open(self, target: str, action: str, now: float) -> bool:
        self._check()
        row = self.db.execute(
            "SELECT COUNT(*) FROM actions WHERE target=? AND action=? AND ts>? AND outcome IN ('no_improvement','reverted')",
            (target, action, now - 86400)).fetchone()
        return row[0] > 0

    def record_action(self, proposal_id: str, run_id: str, target: str, action: str, ts: float) -> None:
        self._check()
        self.db.execute("INSERT OR IGNORE INTO actions VALUES (?,?,?,?,?,?)",
                        (proposal_id, run_id, target, action, ts, "pending"))

    def set_outcome(self, proposal_id: str, outcome: str) -> None:
        self.db.execute("UPDATE actions SET outcome=? WHERE proposal_id=?", (outcome, proposal_id))

    # ---------------------------------------------------------------- leases (one actor per target)
    def acquire_lease(self, target: str, holder: str, now: float, ttl_s: float = 900) -> bool:
        """Atomic compare-and-set: take the lease if it is free, expired, or already ours.

        A single conditional UPSERT, so it is correct across processes sharing the database file, not just
        across threads. (Production: the same statement on Postgres, or an etcd lease.)"""
        self._check()
        with self._lock:
            self.db.execute(
                "INSERT INTO leases (target, holder, expires) VALUES (?, ?, ?) "
                "ON CONFLICT(target) DO UPDATE SET holder = excluded.holder, expires = excluded.expires "
                "WHERE leases.expires <= ? OR leases.holder = excluded.holder",
                (target, holder, now + ttl_s, now))
            row = self.db.execute("SELECT holder FROM leases WHERE target=?", (target,)).fetchone()
            return bool(row and row[0] == holder)

    def release_lease(self, target: str, holder: str) -> None:
        self.db.execute("DELETE FROM leases WHERE target=? AND holder=?", (target, holder))

    # ---------------------------------------------------------------- idempotent execution
    def step_status(self, proposal_id: str, step: str) -> dict | None:
        row = self.db.execute("SELECT status, detail FROM exec_steps WHERE proposal_id=? AND step=?",
                              (proposal_id, step)).fetchone()
        return {"status": row[0], "detail": json.loads(row[1])} if row else None

    def mark_step(self, proposal_id: str, step: str, status: str, detail: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO exec_steps VALUES (?,?,?,?)",
                        (proposal_id, step, status, json.dumps(detail, default=str)))

    # ---------------------------------------------------------------- run persistence
    def save_run(self, run_id: str, incident_id: str, state: str, now: float, data: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?)",
                        (run_id, incident_id, state, now, json.dumps(data, default=str)))

    def list_runs(self) -> list[tuple]:
        return self.db.execute("SELECT run_id, incident_id, state, updated FROM runs ORDER BY updated").fetchall()


class StoreUnavailable(Exception):
    pass
