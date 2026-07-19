"""OpenTelemetry instrumentation for pyharness — the live/aggregate layer.

The durable record (`audit.jsonl` / `trace.jsonl`) is always-on and local; this
module is the *queryable* layer on top. It emits OpenTelemetry traces and metrics
from the three seams every session already has — the LLM client, the broker
chokepoint, and the agent turn — so one run becomes a span tree (turn → code cell
→ llm call / capability call) and aggregate metrics (tokens, cost, latency, call
and denial counts).

Why OTel: the instrumentation is vendor-neutral. Code never imports a backend
SDK; where the data lands is configuration (`OTEL_EXPORTER_OTLP_ENDPOINT`). The
default local backend is a single Phoenix container; swap it for Langfuse,
Grafana, or anything by changing one env var — no code change.

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

import json
import logging
import os
import time
from contextlib import contextmanager

log = logging.getLogger("pyharness.telemetry")

# GenAI semantic-convention attribute names (kept as literals so we don't depend
# on a specific opentelemetry-semantic-conventions version). LLM-aware backends
# (Phoenix, Langfuse) map these into their native cost/token fields automatically.
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


def _endpoint_target() -> tuple[str, int]:
    """Host/port the OTLP gRPC exporter would dial. Falls back to the OTLP gRPC
    default (localhost:4317) when the endpoint is unset — matching the exporter's
    own default, so the pre-flight checks the same address export will use."""
    from urllib.parse import urlparse

    raw = (
        os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        or "http://localhost:4317"
    )
    # urlparse populates hostname/port only with an authority; add a `//` when a
    # bare `host:port` was given (no scheme).
    parsed = urlparse(raw if "//" in raw else f"//{raw}", scheme="http")
    return parsed.hostname or "localhost", parsed.port or 4317


def _endpoint_reachable(host: str, port: int, timeout: float = 0.5) -> bool:
    """A fast TCP connect to decide whether the collector is up. A closed port
    refuses immediately; the short timeout bounds the unreachable-host case."""
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def is_enabled() -> bool:
    return _enabled


def capture_content() -> bool:
    """Whether to attach full prompt/output text to spans (vs. metadata only)."""
    return _capture_content


def setup_telemetry(*, service_name: str | None = None, force: bool = False) -> bool:
    """Configure the global OTel providers from the environment. Idempotent and
    safe to call on every session. Returns whether telemetry is active.

    Env:
      PYHARNESS_TELEMETRY_ENABLED       master switch; an explicit value wins
      OTEL_EXPORTER_OTLP_ENDPOINT       collector endpoint (presence ⇒ enabled
                                        only when the switch is unset)
      OTEL_SERVICE_NAME                 service.name (default "pyharness")
      PYHARNESS_TELEMETRY_CAPTURE_CONTENT  attach prompt/output text (default on)
    """
    global _enabled, _capture_content, _tracer, _meter
    if _enabled and not force:
        return True

    # An explicit PYHARNESS_TELEMETRY_ENABLED wins outright, so a deliberate
    # `false` is never overridden by a leftover endpoint pointing at a backend
    # that isn't running (which otherwise loops "Connection refused" from the
    # OTLP exporter). Endpoint-presence only enables when the switch is unset.
    flag = os.environ.get("PYHARNESS_TELEMETRY_ENABLED", "").strip()
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    want = _truthy(flag) if flag else bool(endpoint)
    if not want:
        _enabled = False
        return False

    # Telemetry is a non-critical sidecar. If the collector isn't up, the OTLP
    # BatchSpanProcessor loops "Connection refused" to stderr (corrupting the
    # interactive REPL, even splicing into streamed output) and blocks the
    # atexit flush on shutdown. Pre-flight the endpoint and, when it's
    # unreachable, degrade to off — one warning, no spam — rather than wire an
    # exporter that can only fail. A restart picks it up once `make up` is live.
    # This is what the module's fail-open contract promises.
    host, port = _endpoint_target()
    if not _endpoint_reachable(host, port):
        log.warning(
            "telemetry endpoint unreachable at %s:%s; traces off this session "
            "(run `make up` to start the collector)",
            host,
            port,
        )
        _enabled = False
        return False

    # Belt-and-suspenders for a backend that dies mid-session (the pre-flight
    # above only covers "never came up"): keep the OTLP exporter's own export
    # retry/failure logging below the console so it can't flood the REPL.
    logging.getLogger("opentelemetry.exporter.otlp").setLevel(logging.CRITICAL)

    # The OTLP exporter uses gRPC, whose C-core logs noisy "FD from fork parent
    # still in poll list" lines when the process forks with a live channel — which
    # the spawn-based kernel child and a Playwright launch both do. Those lines are
    # INFO-level, so fork support alone (which handles the fork correctly) does not
    # quiet them; GRPC_VERBOSITY=ERROR does. setdefault so explicit values win, and
    # both must be set before gRPC initializes, i.e. before the exporter import.
    os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "1")
    os.environ.setdefault("GRPC_VERBOSITY", "ERROR")

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create(
            {
                "service.name": service_name
                or os.environ.get("OTEL_SERVICE_NAME", "pyharness"),
                "service.version": _version(),
            }
        )
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(tracer_provider)
        _tracer = trace.get_tracer("pyharness")

        # Metrics are opt-in. Trace-native backends (the default, Phoenix) don't
        # implement the OTLP metrics service, and per-call cost/tokens/latency
        # already ride on the spans. Enable for a metrics backend (Prometheus via
        # the collector, the Langfuse profile).
        if _truthy(os.environ.get("PYHARNESS_TELEMETRY_METRICS", "false")):
            from opentelemetry import metrics
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
                OTLPMetricExporter,
            )
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

            meter_provider = MeterProvider(
                resource=resource,
                metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())],
            )
            metrics.set_meter_provider(meter_provider)
            _meter = metrics.get_meter("pyharness")
            _init_instruments()

        _capture_content = _truthy(
            os.environ.get("PYHARNESS_TELEMETRY_CAPTURE_CONTENT", "true")
        )
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
        "pyharness.tool.calls",
        unit="1",
        description="Capability calls through the broker",
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
        cm = _tracer.start_as_current_span("pyharness.turn")
        span = cm.__enter__()
        span.set_attribute("session.id", session_id)
        if _capture_content and task:
            span.set_attribute("pyharness.task", task[:4000])
    except Exception:  # noqa: BLE001 — never let span machinery break the turn
        log.debug("turn_span failed", exc_info=True)
        yield None
        return
    try:
        yield span
    except Exception as exc:
        _fail(span, exc, source="turn")
        raise
    finally:
        cm.__exit__(None, None, None)


@contextmanager
def code_cell_span(code: str):
    """Span around one kernel code-cell execution. Capability calls made by the
    cell nest under this automatically (same-thread broker dispatch)."""
    if not _enabled:
        yield None
        return
    try:
        cm = _tracer.start_as_current_span("pyharness.code_cell")
        span = cm.__enter__()
        if _capture_content and code:
            span.set_attribute("pyharness.code", code[:8000])
    except Exception:  # noqa: BLE001
        log.debug("code_cell_span failed", exc_info=True)
        yield None
        return
    try:
        yield span
    finally:
        cm.__exit__(None, None, None)


@contextmanager
def llm_span(
    model: str, tier: str, system: str | None = None, messages: list | None = None
):
    """GenAI client span for one LLM completion. Call `record_llm()` inside once
    usage is known; latency is recorded here on exit.

    When `capture_content()`, the system prompt + message history are attached as
    OpenInference attributes so LLM-aware backends (Phoenix) render them in their
    input-messages panel — the prompt for this call, visible in the trace."""
    if not _enabled:
        yield None
        return
    start = time.perf_counter()
    try:
        cm = _tracer.start_as_current_span(f"chat {model}")
        span = cm.__enter__()
        span.set_attribute(_GEN_AI_SYSTEM, "anthropic")
        span.set_attribute(_GEN_AI_OPERATION, "chat")
        span.set_attribute(_GEN_AI_REQUEST_MODEL, model)
        span.set_attribute("pyharness.tier", tier)
        # OpenInference: mark this as an LLM span and attach the input prompt.
        span.set_attribute("openinference.span.kind", "LLM")
        span.set_attribute("llm.model_name", model)
        if _capture_content:
            _set_input_messages(span, system, messages)
    except Exception:  # noqa: BLE001
        log.debug("llm_span failed", exc_info=True)
        yield None
        return
    try:
        yield span
    except Exception as exc:
        _fail(span, exc, source="llm")
        _add("errors", 1, {"source": "llm"})
        raise
    finally:
        _record(
            "llm_duration", time.perf_counter() - start, {_GEN_AI_REQUEST_MODEL: model}
        )
        cm.__exit__(None, None, None)


def record_llm(
    *,
    model: str,
    tier: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_create: int = 0,
    cost_usd: float = 0.0,
    output_text: str | None = None,
    tool_calls: list | None = None,
) -> None:
    """Attach usage to the current LLM span and record the token/cost/call
    metrics. No-op when telemetry is off.

    When `capture_content()`, the completion text + any tool calls are attached as
    OpenInference output messages so the backend shows the model's reply for this
    call alongside the prompt."""
    if not _enabled:
        return
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        if _capture_content:
            _set_output_message(span, output_text, tool_calls)
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
            _add(
                "llm_tokens",
                cache_read,
                {**by_model, "gen_ai.token.type": "cache_read"},
            )
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
        cm = _tracer.start_as_current_span(f"tool {action}")
        span = cm.__enter__()
        span.set_attribute("pyharness.action", action)
    except Exception:  # noqa: BLE001
        log.debug("tool_span failed", exc_info=True)
        yield None
        return
    try:
        yield span
    finally:
        _record(
            "tool_duration", time.perf_counter() - start, {"pyharness.action": action}
        )
        cm.__exit__(None, None, None)


def record_tool(
    span, *, action: str, decision: str, ok: bool, error: str | None = None
) -> None:
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

# Cap per-message text so a long history can't bloat a span. Generous: prompts
# are the point of this capture.
_MSG_LIMIT = 16000


def _block_text(block) -> str:
    """Flatten one content block (dict or SDK object) to a readable string."""
    t = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
    get = (
        (lambda k, d=None: block.get(k, d))
        if isinstance(block, dict)
        else (lambda k, d=None: getattr(block, k, d))
    )
    if t == "text":
        return get("text", "") or ""
    if t == "tool_use":
        return f"[tool_use {get('name', '')}] {json.dumps(dict(get('input', {})), default=str)}"
    if t == "tool_result":
        return f"[tool_result] {get('content', '')}"
    if t == "thinking":
        return f"[thinking] {get('thinking', '') or ''}"
    return f"[{t}]" if t else str(block)


def _content_to_str(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_block_text(b) for b in content)
    return str(content)


def _set_input_messages(span, system: str | None, messages: list | None) -> None:
    """Attach system + history as OpenInference input messages (Phoenix panel)."""
    idx = 0
    if system:
        span.set_attribute(f"llm.input_messages.{idx}.message.role", "system")
        span.set_attribute(
            f"llm.input_messages.{idx}.message.content", system[:_MSG_LIMIT]
        )
        idx += 1
    for m in messages or []:
        role = (
            m.get("role", "user") if isinstance(m, dict) else getattr(m, "role", "user")
        )
        text = _content_to_str(
            m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
        )
        span.set_attribute(f"llm.input_messages.{idx}.message.role", role)
        span.set_attribute(
            f"llm.input_messages.{idx}.message.content", text[:_MSG_LIMIT]
        )
        idx += 1


def _set_output_message(span, output_text: str | None, tool_calls: list | None) -> None:
    """Attach the completion (text + tool calls) as one OpenInference output message."""
    parts = []
    if output_text:
        parts.append(output_text)
    for i, tc in enumerate(tool_calls or []):
        name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
        inp = tc.get("input") if isinstance(tc, dict) else getattr(tc, "input", {})
        parts.append(f"[tool_use {name}] {json.dumps(dict(inp or {}), default=str)}")
        span.set_attribute(
            f"llm.output_messages.0.message.tool_calls.{i}.tool_call.function.name",
            name or "",
        )
        span.set_attribute(
            f"llm.output_messages.0.message.tool_calls.{i}.tool_call.function.arguments",
            json.dumps(dict(inp or {}), default=str)[:_MSG_LIMIT],
        )
    span.set_attribute("llm.output_messages.0.message.role", "assistant")
    span.set_attribute(
        "llm.output_messages.0.message.content", ("\n".join(parts))[:_MSG_LIMIT]
    )


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
