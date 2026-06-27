"""OpenTelemetry instrumentation for pyharness — the live/aggregate layer.

The durable record (`audit.jsonl` / `trace.jsonl`) is always-on and local; this
module is the *queryable* layer on top. It emits OpenTelemetry traces and metrics
from the three seams every session already has — the LLM client, the broker
chokepoint, and the agent turn — so one run becomes a span tree (turn → code cell
→ llm call / capability call) and aggregate metrics (tokens, cost, latency, call
and denial counts).

Why OTel: the instrumentation is vendor-neutral. Code never imports a backend
SDK; where the data lands is configuration (`OTEL_EXPORTER_OTLP_ENDPOINT`). Swap
Langfuse for Grafana/anything by changing one env var — no code change.

Design rules:
- **Opt-in.** Off unless `PYHARNESS_TELEMETRY_ENABLED` is truthy or an OTLP
  endpoint is set. Off ⇒ every helper here is a cheap no-op; the agent is
  unaffected and the JSONL record is unchanged.
- **Fail-open.** Telemetry must never break the agent. Setup and export failures
  are swallowed; the durable JSONL is the source of truth, not this.
- **Content is sensitive.** Prompts/outputs can carry private data, so attaching
  their full text to spans is gated on `capture_content()` — default on locally,
  set false (redact) in the cloud.

Span nesting is automatic: the broker services capability calls synchronously in
the same thread as the agent loop (see `broker/remote/host.py`), so OTel's
thread-local context nests capability spans under the active code cell with no
explicit propagation.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager

log = logging.getLogger("pyharness.telemetry")

# GenAI semantic-convention attribute names (kept as literals so we don't depend
# on a specific opentelemetry-semantic-conventions version). Backends like
# Langfuse map these automatically.
_GEN_AI_SYSTEM = "gen_ai.system"
_GEN_AI_OPERATION = "gen_ai.operation.name"
_GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
_GEN_AI_USAGE_INPUT = "gen_ai.usage.input_tokens"
_GEN_AI_USAGE_OUTPUT = "gen_ai.usage.output_tokens"

_enabled = False
_capture_content = False
_tracer = None
_meter = None
_instruments: dict[str, object] = {}


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def is_enabled() -> bool:
    return _enabled


def capture_content() -> bool:
    """Whether to attach full prompt/output text to spans (vs. metadata only)."""
    return _capture_content


def setup_telemetry(*, service_name: str | None = None, force: bool = False) -> bool:
    """Configure the global OTel providers from the environment. Idempotent and
    safe to call on every session. Returns whether telemetry is active.

    Env:
      PYHARNESS_TELEMETRY_ENABLED       opt-in flag (or set the endpoint below)
      OTEL_EXPORTER_OTLP_ENDPOINT       collector endpoint (presence ⇒ enabled)
      OTEL_SERVICE_NAME                 service.name (default "pyharness")
      PYHARNESS_TELEMETRY_CAPTURE_CONTENT  attach prompt/output text (default on)
    """
    global _enabled, _capture_content, _tracer, _meter
    if _enabled and not force:
        return True

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    want = _truthy(os.environ.get("PYHARNESS_TELEMETRY_ENABLED", "")) or bool(endpoint)
    if not want:
        _enabled = False
        return False

    try:
        from opentelemetry import metrics, trace
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create(
            {
                "service.name": service_name or os.environ.get("OTEL_SERVICE_NAME", "pyharness"),
                "service.version": _version(),
            }
        )
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(tracer_provider)

        meter_provider = MeterProvider(
            resource=resource,
            metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())],
        )
        metrics.set_meter_provider(meter_provider)

        _tracer = trace.get_tracer("pyharness")
        _meter = metrics.get_meter("pyharness")
        _init_instruments()
        _capture_content = _truthy(os.environ.get("PYHARNESS_TELEMETRY_CAPTURE_CONTENT", "true"))
        _enabled = True
        log.info("telemetry enabled → %s", endpoint or "(default OTLP endpoint)")
    except Exception:  # noqa: BLE001 — telemetry must never break the agent
        log.debug("telemetry setup failed; continuing without it", exc_info=True)
        _enabled = False
    return _enabled


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("pyharness")
    except Exception:  # noqa: BLE001
        return "0.0.0"


def _init_instruments() -> None:
    _instruments["llm_calls"] = _meter.create_counter(
        "pyharness.llm.calls", unit="1", description="LLM completions"
    )
    _instruments["llm_tokens"] = _meter.create_counter(
        "gen_ai.client.token.usage", unit="token", description="LLM tokens by type"
    )
    _instruments["llm_cost"] = _meter.create_counter(
        "pyharness.llm.cost_usd", unit="USD", description="LLM spend (cumulative)"
    )
    _instruments["llm_duration"] = _meter.create_histogram(
        "gen_ai.client.operation.duration", unit="s", description="LLM call latency"
    )
    _instruments["tool_calls"] = _meter.create_counter(
        "pyharness.tool.calls", unit="1", description="Capability calls through the broker"
    )
    _instruments["tool_duration"] = _meter.create_histogram(
        "pyharness.tool.duration", unit="s", description="Capability call latency"
    )
    _instruments["errors"] = _meter.create_counter(
        "pyharness.errors", unit="1", description="Errors by source"
    )


# --- span helpers ---------------------------------------------------------
# Each yields the active span (or None when disabled) and never raises on its
# own account. Telemetry failures are logged at debug and swallowed.


@contextmanager
def turn_span(task: str, session_id: str):
    """Root span for one agent turn. One turn = one trace; `session.id` groups a
    session's turns together in the backend."""
    if not _enabled:
        yield None
        return
    try:
        with _tracer.start_as_current_span("pyharness.turn") as span:
            span.set_attribute("session.id", session_id)
            if _capture_content and task:
                span.set_attribute("pyharness.task", task[:4000])
            try:
                yield span
            except Exception as exc:
                _fail(span, exc, source="turn")
                raise
    except Exception:  # noqa: BLE001 — never let span machinery break the turn
        log.debug("turn_span failed", exc_info=True)
        yield None


@contextmanager
def code_cell_span(code: str):
    """Span around one kernel code-cell execution. Capability calls made by the
    cell nest under this automatically (same-thread broker dispatch)."""
    if not _enabled:
        yield None
        return
    try:
        with _tracer.start_as_current_span("pyharness.code_cell") as span:
            if _capture_content and code:
                span.set_attribute("pyharness.code", code[:8000])
            yield span
    except Exception:  # noqa: BLE001
        log.debug("code_cell_span failed", exc_info=True)
        yield None


@contextmanager
def llm_span(model: str, tier: str):
    """GenAI client span for one LLM completion. Call `record_llm()` inside once
    usage is known; latency is recorded here on exit."""
    if not _enabled:
        yield None
        return
    start = time.perf_counter()
    try:
        with _tracer.start_as_current_span(f"chat {model}") as span:
            span.set_attribute(_GEN_AI_SYSTEM, "anthropic")
            span.set_attribute(_GEN_AI_OPERATION, "chat")
            span.set_attribute(_GEN_AI_REQUEST_MODEL, model)
            span.set_attribute("pyharness.tier", tier)
            try:
                yield span
            except Exception as exc:
                _fail(span, exc, source="llm")
                _add("errors", 1, {"source": "llm"})
                raise
            finally:
                _record("llm_duration", time.perf_counter() - start, {_GEN_AI_REQUEST_MODEL: model})
    except Exception:  # noqa: BLE001
        log.debug("llm_span failed", exc_info=True)
        yield None


def record_llm(
    *,
    model: str,
    tier: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_create: int = 0,
    cost_usd: float = 0.0,
) -> None:
    """Attach usage to the current LLM span and record the token/cost/call
    metrics. No-op when telemetry is off."""
    if not _enabled:
        return
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        span.set_attribute(_GEN_AI_USAGE_INPUT, input_tokens)
        span.set_attribute(_GEN_AI_USAGE_OUTPUT, output_tokens)
        span.set_attribute("gen_ai.usage.cache_read_input_tokens", cache_read)
        span.set_attribute("gen_ai.usage.cache_creation_input_tokens", cache_create)
        span.set_attribute("pyharness.cost_usd", round(cost_usd, 6))

        by_model = {_GEN_AI_REQUEST_MODEL: model, "pyharness.tier": tier}
        _add("llm_calls", 1, by_model)
        _add("llm_cost", cost_usd, by_model)
        _add("llm_tokens", input_tokens, {**by_model, "gen_ai.token.type": "input"})
        _add("llm_tokens", output_tokens, {**by_model, "gen_ai.token.type": "output"})
        if cache_read:
            _add("llm_tokens", cache_read, {**by_model, "gen_ai.token.type": "cache_read"})
    except Exception:  # noqa: BLE001
        log.debug("record_llm failed", exc_info=True)


@contextmanager
def tool_span(action: str):
    """Span around one capability call. The broker sets the decision/result via
    `record_tool()`."""
    if not _enabled:
        yield None
        return
    start = time.perf_counter()
    try:
        with _tracer.start_as_current_span(f"tool {action}") as span:
            span.set_attribute("pyharness.action", action)
            try:
                yield span
            finally:
                _record("tool_duration", time.perf_counter() - start, {"pyharness.action": action})
    except Exception:  # noqa: BLE001
        log.debug("tool_span failed", exc_info=True)
        yield None


def record_tool(span, *, action: str, decision: str, ok: bool, error: str | None = None) -> None:
    """Record the outcome of a capability call on its span and the call/denial
    metrics. `span` may be None (telemetry off)."""
    if not _enabled:
        return
    try:
        attrs = {"pyharness.action": action, "decision": decision, "ok": ok}
        _add("tool_calls", 1, attrs)
        if not ok:
            _add("errors", 1, {"source": "tool", "decision": decision})
        if span is not None:
            span.set_attribute("decision", decision)
            span.set_attribute("ok", ok)
            if error:
                span.set_attribute("error", error[:500])
                _set_error_status(span)
    except Exception:  # noqa: BLE001
        log.debug("record_tool failed", exc_info=True)


def record_budget(span, *, spent_usd: float, calls: int) -> None:
    """Stamp the final budget snapshot on the turn span."""
    if not _enabled or span is None:
        return
    try:
        span.set_attribute("pyharness.budget.spent_usd", round(spent_usd, 6))
        span.set_attribute("pyharness.budget.calls", calls)
    except Exception:  # noqa: BLE001
        log.debug("record_budget failed", exc_info=True)


# --- internals ------------------------------------------------------------


def _add(name: str, amount, attrs: dict) -> None:
    inst = _instruments.get(name)
    if inst is not None and amount:
        inst.add(amount, attrs)


def _record(name: str, amount, attrs: dict) -> None:
    inst = _instruments.get(name)
    if inst is not None:
        inst.record(amount, attrs)


def _fail(span, exc: Exception, *, source: str) -> None:
    try:
        span.record_exception(exc)
        _set_error_status(span)
    except Exception:  # noqa: BLE001
        pass


def _set_error_status(span) -> None:
    from opentelemetry.trace import Status, StatusCode

    span.set_status(Status(StatusCode.ERROR))
