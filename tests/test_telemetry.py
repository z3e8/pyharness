"""Telemetry is exercised with in-memory OTel exporters: no collector, no
network. We assert spans nest into one trace with the right attributes, the
metrics fire, and everything is a clean no-op when disabled."""

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from pyharness import telemetry


def _enable_in_memory():
    exporter = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    reader = InMemoryMetricReader()
    mp = MeterProvider(metric_readers=[reader])
    telemetry._enabled = True
    telemetry._capture_content = True
    telemetry._tracer = tp.get_tracer("test")
    telemetry._meter = mp.get_meter("test")
    telemetry._instruments.clear()
    telemetry._init_instruments()
    return exporter, reader


def _metric_names(reader):
    data = reader.get_metrics_data()
    return {
        m.name
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
    }


def test_spans_nest_into_one_trace_with_metrics(tmp_path):
    exporter, reader = _enable_in_memory()
    try:
        with telemetry.turn_span("do a thing", "sess-1") as turn:
            with telemetry.llm_span("claude-haiku-4-5", "cheap"):
                telemetry.record_llm(
                    model="claude-haiku-4-5",
                    tier="cheap",
                    input_tokens=10,
                    output_tokens=5,
                    cache_read=3,
                    cost_usd=0.0012,
                )
            with telemetry.code_cell_span("write('x', 'y')"):
                with telemetry.tool_span("files.write") as ts:
                    telemetry.record_tool(ts, action="files.write", decision="allow", ok=True)
            telemetry.record_budget(turn, spent_usd=0.0012, calls=1)

        spans = exporter.get_finished_spans()
        by_name = {s.name: s for s in spans}
        assert "pyharness.turn" in by_name
        assert "chat claude-haiku-4-5" in by_name
        assert "pyharness.code_cell" in by_name
        assert "tool files.write" in by_name

        # One trace for the whole turn.
        assert len({s.context.trace_id for s in spans}) == 1

        # The capability call nests under the code cell (same-thread dispatch).
        assert by_name["tool files.write"].parent.span_id == by_name["pyharness.code_cell"].context.span_id

        # GenAI + custom attributes landed on the LLM span.
        llm = by_name["chat claude-haiku-4-5"]
        assert llm.attributes["gen_ai.system"] == "anthropic"
        assert llm.attributes["gen_ai.usage.input_tokens"] == 10
        assert llm.attributes["pyharness.cost_usd"] == 0.0012

        # Turn carries session + budget; capture_content attached the task text.
        turn_span = by_name["pyharness.turn"]
        assert turn_span.attributes["session.id"] == "sess-1"
        assert turn_span.attributes["pyharness.budget.spent_usd"] == 0.0012
        assert turn_span.attributes["pyharness.task"] == "do a thing"

        names = _metric_names(reader)
        assert {"pyharness.llm.calls", "pyharness.llm.cost_usd", "gen_ai.client.token.usage",
                "pyharness.tool.calls"} <= names
    finally:
        telemetry._enabled = False


def test_denied_tool_records_error(tmp_path):
    exporter, reader = _enable_in_memory()
    try:
        with telemetry.tool_span("shell.bash") as ts:
            telemetry.record_tool(ts, action="shell.bash", decision="deny", ok=False)
        span = exporter.get_finished_spans()[0]
        assert span.attributes["decision"] == "deny"
        assert span.attributes["ok"] is False
        assert "pyharness.errors" in _metric_names(reader)
    finally:
        telemetry._enabled = False


def test_helpers_are_noops_when_disabled():
    telemetry._enabled = False
    with telemetry.turn_span("t", "s") as span:
        assert span is None
    with telemetry.llm_span("m", "cheap") as span:
        assert span is None
    with telemetry.tool_span("files.read") as span:
        assert span is None
    # Recorders must not raise when off.
    telemetry.record_llm(model="m", tier="cheap", input_tokens=1, output_tokens=1)
    telemetry.record_tool(None, action="a", decision="allow", ok=True)
    telemetry.record_budget(None, spent_usd=0.0, calls=0)


def test_setup_is_opt_in(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("PYHARNESS_TELEMETRY_ENABLED", raising=False)
    telemetry._enabled = False
    assert telemetry.setup_telemetry() is False
    assert telemetry.is_enabled() is False
