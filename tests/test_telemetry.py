"""Telemetry is exercised with in-memory OTel exporters: no collector, no
network. We assert spans nest into one trace with the right attributes, the
metrics fire, and everything is a clean no-op when disabled."""

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from pyharness.obs import telemetry


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


def test_llm_span_captures_prompt_and_completion(tmp_path):
    exporter, _ = _enable_in_memory()
    try:
        messages = [
            {"role": "user", "content": "find the bug"},
            {"role": "assistant", "content": [{"type": "text", "text": "checking"}]},
            {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
        ]
        with telemetry.llm_span("claude-haiku-4-5", "cheap", system="be helpful", messages=messages):
            telemetry.record_llm(
                model="claude-haiku-4-5",
                tier="cheap",
                input_tokens=1,
                output_tokens=1,
                output_text="here it is",
                tool_calls=[{"name": "run_python", "input": {"code": "print(1)"}}],
            )
        attrs = exporter.get_finished_spans()[0].attributes
        # OpenInference markers so Phoenix renders the messages panel.
        assert attrs["openinference.span.kind"] == "LLM"
        # System prompt is message 0, then the history in order.
        assert attrs["llm.input_messages.0.message.role"] == "system"
        assert attrs["llm.input_messages.0.message.content"] == "be helpful"
        assert attrs["llm.input_messages.1.message.content"] == "find the bug"
        assert "checking" in attrs["llm.input_messages.2.message.content"]
        # Completion text + tool call on the output side.
        assert "here it is" in attrs["llm.output_messages.0.message.content"]
        assert attrs["llm.output_messages.0.message.tool_calls.0.tool_call.function.name"] == "run_python"
    finally:
        telemetry._enabled = False


def test_content_capture_off_omits_prompt(tmp_path):
    exporter, _ = _enable_in_memory()
    telemetry._capture_content = False
    try:
        with telemetry.llm_span("m", "cheap", system="secret", messages=[{"role": "user", "content": "hi"}]):
            telemetry.record_llm(model="m", tier="cheap", input_tokens=1, output_tokens=1, output_text="x")
        attrs = exporter.get_finished_spans()[0].attributes
        assert "llm.input_messages.0.message.content" not in attrs
        assert "llm.output_messages.0.message.content" not in attrs
    finally:
        telemetry._enabled = False
        telemetry._capture_content = True


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


def test_body_exception_propagates_through_span(tmp_path):
    # A span guards its own machinery, not the body: an exception raised inside
    # the `with` must come back out unchanged. Regression for the swallowing
    # `except` that re-yielded and raised "generator didn't stop after throw()".
    exporter, _ = _enable_in_memory()
    try:
        class Boom(Exception):
            pass

        for span_cm in (
            telemetry.turn_span("t", "s"),
            telemetry.code_cell_span("code"),
            telemetry.llm_span("m", "cheap"),
            telemetry.tool_span("skills.save_skill"),
        ):
            with pytest.raises(Boom):
                with span_cm:
                    raise Boom("from body")
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


def test_explicit_false_wins_over_endpoint(monkeypatch):
    # A deliberate PYHARNESS_TELEMETRY_ENABLED=false must not be overridden by a
    # leftover endpoint pointing at a backend that isn't running.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    monkeypatch.setenv("PYHARNESS_TELEMETRY_ENABLED", "false")
    telemetry._enabled = False
    assert telemetry.setup_telemetry() is False
    assert telemetry.is_enabled() is False


def test_unreachable_endpoint_degrades_to_off(monkeypatch):
    # Telemetry deliberately on, but the collector isn't up (port 1 is refused
    # immediately). Rather than wire an OTLP exporter that can only loop
    # "Connection refused" into the REPL, setup pre-flights and stays off.
    monkeypatch.setenv("PYHARNESS_TELEMETRY_ENABLED", "true")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")
    telemetry._enabled = False
    assert telemetry.setup_telemetry() is False
    assert telemetry.is_enabled() is False


def test_endpoint_target_parses_scheme_and_bare_host(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.local:4318")
    assert telemetry._endpoint_target() == ("collector.local", 4318)
    # A bare host:port (no scheme) still resolves; a traces-specific override wins.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "otel:9999")
    assert telemetry._endpoint_target() == ("otel", 9999)
    # Unset falls back to the OTLP gRPC default the exporter itself would use.
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    assert telemetry._endpoint_target() == ("localhost", 4317)
