"""Time formatting shared by the simulator, tools and reports."""

from __future__ import annotations

from datetime import datetime, timezone


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
