"""Append-only, hash-chained audit log.

Each record: run_id, seq, ts, actor (agent|policy|human|executor|system), event, payload,
versions, prev_hash, hash. The hash chain makes edits evident; chain heads are appended to
runs/anchors.jsonl, which stands in for WORM storage (S3 Object Lock) so a deleted run is
detectable too.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path


def _canon(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


class AuditLog:
    def __init__(self, runs_dir: Path, run_id: str, versions: dict) -> None:
        self.dir = runs_dir / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "audit.jsonl"
        self.anchor_path = runs_dir / "anchors.jsonl"
        self.run_id = run_id
        self.versions = versions
        self.seq = 0
        self.prev = "0" * 64

    def append(self, actor: str, event: str, payload: dict | None = None, sim_time: float | None = None) -> dict:
        self.seq += 1
        rec = {"run_id": self.run_id, "seq": self.seq, "ts": time.time(), "sim_time": sim_time, "actor": actor,
               "event": event, "payload": payload or {}, "versions": self.versions, "prev_hash": self.prev}
        rec["hash"] = hashlib.sha256(_canon(rec).encode()).hexdigest()
        self.prev = rec["hash"]
        with self.path.open("a") as f:
            f.write(_canon(rec) + "\n")
        return rec

    def anchor(self) -> None:
        with self.anchor_path.open("a") as f:
            f.write(_canon({"run_id": self.run_id, "head": self.prev, "records": self.seq, "ts": time.time()}) + "\n")


def verify_chain(path: Path) -> tuple[bool, str]:
    prev = "0" * 64
    n = 0
    for line in path.read_text().splitlines():
        rec = json.loads(line)
        h = rec.pop("hash")
        if rec["prev_hash"] != prev:
            return False, f"chain broken at seq {rec['seq']} (prev_hash mismatch)"
        if hashlib.sha256(_canon(rec).encode()).hexdigest() != h:
            return False, f"record seq {rec['seq']} was modified"
        prev = h
        n += 1
    anchors = path.parent.parent / "anchors.jsonl"
    if anchors.exists():
        heads = [json.loads(x) for x in anchors.read_text().splitlines()]
        mine = [a for a in heads if a["run_id"] == path.parent.name]
        if mine and mine[-1]["head"] != prev:
            return False, "chain head does not match the anchored head (records removed or appended)"
    return True, f"{n} records, chain intact, head {prev[:16]}"
