"""OpenTelemetry tracing for agent runs: one trace per run.

Span names follow the plan: asap.incident > asap.state.<STATE> > gen_ai.chat | asap.tool.<name> |
asap.policy.eval | asap.approval | asap.execute. Spans are written as JSON lines to
runs/<run_id>/spans.jsonl; set OTEL_EXPORTER_OTLP_ENDPOINT and install
opentelemetry-exporter-otlp to also ship them to Jaeger/Tempo.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Sequence

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult


class JsonlSpanExporter(SpanExporter):
    """Routes each span to its run's spans.jsonl by trace_id, so concurrent runs never interleave."""

    def __init__(self) -> None:
        self.paths: dict[int, Path] = {}
        self._lock = threading.Lock()

    def register(self, trace_id: int, path: Path) -> None:
        with self._lock:
            self.paths[trace_id] = path

    def unregister(self, trace_id: int) -> None:
        with self._lock:
            self.paths.pop(trace_id, None)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        for s in spans:
            path = self.paths.get(s.context.trace_id)
            if path is None:
                continue
            with path.open("a") as f:
                f.write(json.dumps({
                    "name": s.name, "trace_id": f"{s.context.trace_id:032x}", "span_id": f"{s.context.span_id:016x}",
                    "parent_span_id": f"{s.parent.span_id:016x}" if s.parent else None,
                    "start_ns": s.start_time, "end_ns": s.end_time,
                    "duration_ms": round((s.end_time - s.start_time) / 1e6, 2) if s.end_time else None,
                    "status": s.status.status_code.name, "attributes": dict(s.attributes or {}),
                }, default=str) + "\n")
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        pass


_exporter = JsonlSpanExporter()
_provider: TracerProvider | None = None


def setup() -> trace.Tracer:
    global _provider
    if _provider is None:
        _provider = TracerProvider(resource=Resource.create({"service.name": "asap-agent"}))
        _provider.add_span_processor(SimpleSpanProcessor(_exporter))
        if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

                _provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter()))
            except ImportError:
                pass
    return _provider.get_tracer("asap")


def register_run(trace_id: int, path: Path) -> None:
    _exporter.register(trace_id, path)


def unregister_run(trace_id: int) -> None:
    _exporter.unregister(trace_id)
