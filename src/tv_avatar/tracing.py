"""OpenTelemetry bootstrap and the span vocabulary (observability plan, D13–D21).

OpenTelemetry is the only instrumentation API; Langfuse is an OTLP/HTTP sink,
never an SDK. Pipecat's own tracing emits ``conversation → turn → stt/tts``;
everything of ours hangs under those through ``observation()`` and carries
its Langfuse observation type, filterable ``langfuse.observation.metadata.*``
keys and ``tv.*`` details from the constants below — no attribute key is a
string literal in business code.

Tracing off means no provider: every ``observation()`` yields OTel's no-op
span and nothing in the media path changes. The toggle lives here and in the
pipeline builder/runner only.

This module imports no Pipecat symbol: ``memory/`` and ``recs/`` use it and
know nothing about the pipeline.
"""
from __future__ import annotations

import asyncio
import base64
import contextvars
import os
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from loguru import logger
from opentelemetry import baggage, context, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.processor.baggage import ALLOW_ALL_BAGGAGE_KEYS, BaggageSpanProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import (
    Event,
    ReadableSpan,
    Span,
    TracerProvider,
)
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import (
    Link,
    NonRecordingSpan,
    SpanContext,
    Status,
    StatusCode,
    Tracer,
)

from tv_avatar.telemetry import ContentPolicy

if TYPE_CHECKING:
    from tv_avatar.config import Settings
    from tv_avatar.session.state import SessionState

#: Where media-path code finds Pipecat's current turn span: a callable, because
#: the observers and taps are built before the task that owns the turn tracker.
TurnContextFn = Callable[[], "SpanContext | None"]

# --- Baggage: trace identity on every span (D14, D20) --------------------------
BAGGAGE_SESSION_ID = "langfuse.session.id"
BAGGAGE_USER_ID = "langfuse.user.id"
BAGGAGE_TRACE_NAME = "langfuse.trace.name"
BAGGAGE_TRACE_META_AVATAR = "langfuse.trace.metadata.avatar_id"
BAGGAGE_TRACE_META_LANGUAGE = "langfuse.trace.metadata.language"
BAGGAGE_TRACE_META_AGENT = "langfuse.trace.metadata.agent_impl"
BAGGAGE_TRACE_META_HALF_DUPLEX = "langfuse.trace.metadata.half_duplex"
TRACE_NAME = "tv-avatar-session"
ATTR_SESSION_ID = BAGGAGE_SESSION_ID

# --- Langfuse observation-level mapping ---------------------------------------
ATTR_OBS_TYPE = "langfuse.observation.type"
ATTR_OBS_LEVEL = "langfuse.observation.level"
ATTR_OBS_STATUS_MESSAGE = "langfuse.observation.status_message"
ATTR_OBS_INPUT = "langfuse.observation.input"
ATTR_OBS_OUTPUT = "langfuse.observation.output"
ATTR_TRACE_INPUT = "langfuse.trace.input"
ATTR_TRACE_OUTPUT = "langfuse.trace.output"

OBS_TYPE_SPAN = "span"
OBS_TYPE_GENERATION = "generation"
OBS_TYPE_TOOL = "tool"
OBS_TYPE_RETRIEVER = "retriever"
OBS_TYPE_EVENT = "event"
OBS_TYPE_AGENT = "agent"
ATTR_RELEASE = "langfuse.release"
ATTR_TELEMETRY_VERSION = "langfuse.observation.metadata.telemetry_version"
TELEMETRY_VERSION = "2"

LEVEL_WARNING = "WARNING"
LEVEL_ERROR = "ERROR"

# --- Filterable facts: langfuse.observation.metadata.<key> (D20) --------------
_META = "langfuse.observation.metadata."
META_INTENT = _META + "intent"
META_OPERATION = _META + "operation"
META_CYCLES = _META + "cycles"
META_CYCLE = _META + "cycle"
META_FALLBACK = _META + "fallback"
META_INTERRUPTED = _META + "interrupted"
META_GREETING = _META + "greeting"
META_SOURCE = _META + "source"
META_OVER_BUDGET = _META + "over_budget"
META_VERB = _META + "verb"
META_KIND = _META + "kind"
META_STATUS = _META + "status"
META_TRIGGER = _META + "trigger"
META_REFRESH_TRIGGERED = _META + "refresh_triggered"
META_QUERY_EMBED = _META + "query_embed"
META_N_ERRORS = _META + "n_errors"
META_TURN_ID = _META + "turn_id"
META_ACTION_INDEX = _META + "action_index"
META_SCHEMA_NAME = _META + "schema_name"
META_SCHEMA_HASH = _META + "schema_hash"
META_FINAL_CYCLE = _META + "final_cycle"
META_VALIDATION = _META + "validation"
META_STOP_REASON = _META + "stop_reason"
META_FINISH_REASON = _META + "finish_reason"
META_USAGE_AVAILABLE = _META + "usage_available"
META_PROVIDER = _META + "provider"
META_REPLY_TYPE = _META + "reply_type"
ATTR_RESPONSE_MODEL = "gen_ai.response.model"
ATTR_RESPONSE_ID = "gen_ai.response.id"
ATTR_REQUEST_ID = "tv.provider.request_id"
ATTR_FIRST_CONTENT_MS = "tv.cycle.first_content_ms"
ATTR_GENERATION_MS = "tv.cycle.generation_ms"
ATTR_CYCLE_TOTAL_MS = "tv.cycle.total_ms"
ATTR_FIRST_SENTENCE_MS = "tv.cycle.first_sentence_ms"
ATTR_SENTENCE_INDEX = "tv.speech.sentence_index"
ATTR_SPEECH_SUBMITTED = "tv.speech.submitted"
ATTR_PROMPT_HASH = "tv.prompt.hash"
ATTR_ENVELOPE_COMPLETE = "tv.cycle.envelope_complete"
ATTR_ERROR_TYPE = "error.type"

# --- gen_ai.* (Langfuse maps these to model / parameters / usage) -------------
ATTR_GENAI_MODEL = "gen_ai.request.model"
ATTR_GENAI_TEMPERATURE = "gen_ai.request.temperature"
ATTR_GENAI_MAX_TOKENS = "gen_ai.request.max_tokens"
ATTR_GENAI_INPUT_TOKENS = "gen_ai.usage.input_tokens"
ATTR_GENAI_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"

# --- tv.* details: read on the span, not filtered across spans ----------------
ATTR_TURN_ID = "tv.turn_id"
ATTR_TURN_PREFIX = "tv.turn."
ATTR_TURN_DROPPED_COMMANDS = "tv.turn.dropped_commands"
ATTR_TURN_OFFERED_IDS = "tv.turn.offered_ids"
ATTR_MEMORY_EMPTY = "tv.memory.empty"
ATTR_MEMORY_STALE = "tv.memory.stale"
ATTR_MEMORY_TOKEN_EST = "tv.memory.token_est"
ATTR_MEMORY_WAITED_MS = "tv.memory.waited_ms"
ATTR_MEMORY_CHARS = "tv.memory.chars"
ATTR_MEMORY_PREFIX_REUSED = "tv.memory.prefix_reused"
ATTR_MEMORY_PENDING_TURNS = "tv.memory.pending_turns"
ATTR_MEMORY_INTERRUPTED = "tv.memory.interrupted"
ATTR_MEMORY_SAID_CHARS = "tv.memory.said_chars"
ATTR_MEMORY_TURNS = "tv.memory.turns"
ATTR_MEMORY_PROFILE_CHARS = "tv.memory.profile_chars"
ATTR_HISTORY_CHARS = "tv.history.chars"
ATTR_CYCLE_MAX = "tv.cycle.max"
ATTR_CYCLE_TTFT_MS = "tv.cycle.ttft_ms"
ATTR_CYCLE_BUDGET_MS = "tv.cycle.budget_ms"
ATTR_CYCLE_N_ACTIONS = "tv.cycle.n_actions"
ATTR_CYCLE_N_REJECTED = "tv.cycle.n_rejected"
EVENT_ACTION_REJECTED = "tv.action.rejected"
ATTR_ACTION_AWAITS_RESULT = "tv.action.awaits_result"
ATTR_ACTION_RETURNS_OBSERVATION = "tv.action.returns_observation"
ATTR_ACTION_MS = "tv.action.ms"
ATTR_ACTION_N_TITLES = "tv.action.n_titles"
ATTR_ACTION_MATCHED = "tv.action.matched"
ATTR_FALLBACK_N_ACTIONS = "tv.fallback.n_actions"
ATTR_RECS_CHANNELS = "tv.recs.channels"
ATTR_RECS_N = "tv.recs.n"
ATTR_RECS_LIMIT = "tv.recs.limit"
ATTR_COMMAND_ID = "tv.command.id"
ATTR_COMMAND_AWAITS_RESULT = "tv.command.awaits_result"
ATTR_COMMAND_ROUNDTRIP_MS = "tv.command.roundtrip_ms"
EVENT_TURN_CANCELLED = "tv.turn.cancelled"
ATTR_LATENCY_PREFIX = "tv.latency."
EVENT_ERROR = "tv.error"

#: What ``TRACE_CONTENT=false`` strips from every span before export (D18):
#: Langfuse's input/output mapping plus Pipecat's own content attributes.
CONTENT_ATTRS: frozenset[str] = frozenset({
    ATTR_OBS_INPUT, ATTR_OBS_OUTPUT, ATTR_TRACE_INPUT, ATTR_TRACE_OUTPUT,
    "gen_ai.prompt", "gen_ai.completion", "gen_ai.input.messages", "gen_ai.output.messages",
    "gen_ai.system_instructions", "input.value", "output.value",
    "messages", "input", "output", "system", "system_instructions", "transcript", "text",
    "message", "context_messages", "param.system_instruction",
    "exception.message", "exception.stacktrace", ATTR_OBS_STATUS_MESSAGE,
    "reason", "args", "raw", "say", "query", "error", "payload", ATTR_SPEECH_SUBMITTED,
})

_provider: TracerProvider | None = None


def tracer() -> Tracer:
    return trace.get_tracer("tv_avatar")


def tracing_wanted(settings: Settings) -> bool:
    """Enabled *and* pointed at a sink. Enabled-but-unconfigured warns once."""
    if not settings.tracing_enabled:
        return False
    if settings.otel_exporter_otlp_endpoint or (
        settings.langfuse_public_key and settings.langfuse_secret_key
    ):
        return True
    logger.warning("TRACING_ENABLED=true but neither Langfuse keys nor "
                   "OTEL_EXPORTER_OTLP_ENDPOINT are set; tracing stays off")
    return False


def _with_traces_path(endpoint: str) -> str:
    # An explicit ``endpoint=`` is used verbatim by the HTTP exporter (verified on
    # 1.44.0); only the OTEL_EXPORTER_OTLP_ENDPOINT env path appends the signal
    # suffix itself. Without it every batch would POST to the collector root and
    # fail silently inside the export thread.
    endpoint = endpoint.rstrip("/")
    return endpoint if endpoint.endswith("/v1/traces") else endpoint + "/v1/traces"


def build_exporter(settings: Settings) -> SpanExporter:
    """OTLP/HTTP exporter: a raw collector override wins over the Langfuse keys.

    Langfuse ingests OTLP over HTTP only (gRPC is rejected). Headers go in as a
    dict — the env-var route would need the ``Basic%20`` URL-encoding trap.
    """
    if settings.otel_exporter_otlp_endpoint:
        headers = dict(
            kv.split("=", 1) for kv in settings.otel_exporter_otlp_headers.split(",") if "=" in kv
        )
        return OTLPSpanExporter(
            endpoint=_with_traces_path(settings.otel_exporter_otlp_endpoint), headers=headers)
    token = base64.b64encode(
        f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()).decode()
    return OTLPSpanExporter(
        endpoint=_with_traces_path(f"{settings.langfuse_host.rstrip('/')}/api/public/otel"),
        headers={
            "Authorization": f"Basic {token}",
            # Without it directly-ingested OTel data can lag up to 10 minutes and
            # look "missing" — the first thing to check when no trace appears.
            "x-langfuse-ingestion-version": "4",
        },
    )


def setup_tracing(settings: Settings, *, exporter: SpanExporter | None = None) -> bool:
    """Install the process-global provider. True only when *this call* installed
    it — the caller that gets True owns the matching ``shutdown_tracing()``, so
    an app booted under an already-traced process (the test fixture) leaves
    that provider alone.

    Built here rather than through ``pipecat.utils.tracing.setup`` because that
    helper registers its batch processor around the raw exporter, and the
    redacting exporter (D18) has to sit *inside* it. The resource mirrors
    Pipecat's so its spans and ours share one service.
    """
    global _provider
    if exporter is None and not tracing_wanted(settings):
        return False
    if _provider is not None:
        return False
    if exporter is None:
        exporter = build_exporter(settings)
    provider = TracerProvider(resource=Resource.create({
        "service.name": settings.otel_service_name,
        "service.instance.id": os.getenv("HOSTNAME", "unknown"),
        "deployment.environment": os.getenv("ENVIRONMENT", "development"),
    }))
    # Baggage runs in on_start, so it is registered whether or not content is redacted.
    provider.add_span_processor(BaggageSpanProcessor(ALLOW_ALL_BAGGAGE_KEYS))
    policy = ContentPolicy.from_settings(settings)
    provider.add_span_processor(BatchSpanProcessor(RedactingSpanExporter(exporter, policy=policy)))
    if settings.otel_console_export:
        provider.add_span_processor(BatchSpanProcessor(
            RedactingSpanExporter(ConsoleSpanExporter(), policy=policy)))
    trace.set_tracer_provider(provider)
    _provider = provider
    logger.info("tracing on: service={} content={}", settings.otel_service_name,
                settings.trace_content)
    return True


def shutdown_tracing() -> None:
    """Flush the last batch — otherwise the final turn's spans die with the process."""
    global _provider
    if _provider is None:
        return
    _provider.force_flush()
    _provider.shutdown()
    _provider = None


def session_attributes(session: SessionState, settings: Settings,
                       *, half_duplex: bool = False) -> dict[str, str]:
    """The identity dict: baggage for ``session_scope`` and, belt-and-braces,
    ``PipelineTask(additional_span_attributes=...)`` for the conversation span."""
    return {
        BAGGAGE_SESSION_ID: session.session_id,
        BAGGAGE_USER_ID: session.user_id,
        BAGGAGE_TRACE_NAME: TRACE_NAME,
        BAGGAGE_TRACE_META_AVATAR: session.persona.avatar.id,
        BAGGAGE_TRACE_META_LANGUAGE: session.persona.language.code,
        BAGGAGE_TRACE_META_AGENT: settings.agent_impl,
        BAGGAGE_TRACE_META_HALF_DUPLEX: str(half_duplex).lower(),
        ATTR_RELEASE: settings.app_revision,
        ATTR_TELEMETRY_VERSION: TELEMETRY_VERSION,
    }


@contextmanager
def session_scope(session: SessionState, settings: Settings,
                  *, half_duplex: bool = False) -> Iterator[None]:
    """Attach the session identity as OTel baggage for everything created inside.

    Ids and enum-ish strings only — baggage propagates on outbound HTTP by
    default, so nothing sensitive and no content may ride on it.
    """
    ctx = context.get_current()
    for key, value in session_attributes(session, settings, half_duplex=half_duplex).items():
        ctx = baggage.set_baggage(key, value, context=ctx)
    token = context.attach(ctx)
    try:
        yield
    finally:
        context.detach(token)


@contextmanager
def attribute_scope(attributes: dict[str, Any]) -> Iterator[None]:
    ctx = context.get_current()
    for key, value in attributes.items():
        ctx = baggage.set_baggage(key, value, context=ctx)
    token = context.attach(ctx)
    try:
        yield
    finally:
        context.detach(token)


@contextmanager
def observation(name: str, *, type: str, parent_context: context.Context | None = None,
                links: tuple[Link, ...] = (), **attributes: Any) -> Iterator[Span]:
    """The one way business code opens a span, so the Langfuse type is never forgotten.

    ``attributes`` are set at open; ``None`` values are dropped (OTel rejects them).
    """
    attrs = {ATTR_OBS_TYPE: type}
    attrs.update({k: v for k, v in attributes.items() if v is not None})
    with tracer().start_as_current_span(
            name, attributes=attrs, context=parent_context, links=links,
            record_exception=False, set_status_on_exception=False) as span:
        try:
            yield span
        except asyncio.CancelledError:
            span.set_attribute(META_STATUS, "cancelled")
            logger.info("operation.cancelled", event="operation.cancelled", operation=name)
            raise
        except Exception as err:
            span.set_attribute(META_STATUS, "error")
            span.set_attribute(ATTR_OBS_LEVEL, LEVEL_ERROR)
            logger.error("operation.failed", event="operation.failed", operation=name,
                         error_type=err.__class__.__name__)
            span.set_status(StatusCode.ERROR, err.__class__.__name__)
            span.set_attribute(ATTR_ERROR_TYPE, err.__class__.__name__)
            span.add_event("exception", {"exception.type": err.__class__.__name__})
            raise


def set_attributes(span: Span, attributes: dict[str, Any]) -> None:
    """``span.set_attributes`` minus ``None`` values, in one place."""
    span.set_attributes({k: v for k, v in attributes.items() if v is not None})


def task_context(span_context: SpanContext | None) -> contextvars.Context:
    """A ``contextvars.Context`` for ``create_task(context=...)`` whose current
    span is ``span_context`` (baggage kept). For work fired from the media path
    — observers and taps — which runs outside the turn's own context: without
    this, each such task is a root span and shows up as its own trace."""
    ctx = contextvars.copy_context()
    if span_context is not None:
        ctx.run(context.attach, trace.set_span_in_context(NonRecordingSpan(span_context)))
    return ctx


def detached_from_span() -> context.Context:
    """The current context minus its span: a root span that keeps the baggage.

    For work that outlives the turn (mid-session memory folds) — parenting it
    on a long-gone ``llm`` span would be wrong, losing the session id worse.
    """
    return trace.set_span_in_context(trace.INVALID_SPAN, context.get_current())


class RedactingSpanExporter(SpanExporter):
    """Apply the content policy to every span before the wrapped exporter sees it.

    An exporter rather than a ``SpanProcessor``: ``export`` runs on the batch
    processor's worker thread, whereas a processor's ``on_end`` runs inside
    ``span.end()`` on the ending task — the JSON parse and rebuild per span
    (the whole prompt on ``agent.cycle``) would land on the turn's own path.
    A ``ReadableSpan`` is immutable, so a copy with filtered attributes is
    forwarded — the SDK's constructor takes every field it exposes (verified
    on opentelemetry-sdk 1.44.0). Wrapping the exporter, inside the batch
    processor, is what makes the policy hold for Pipecat's spans as well as ours.
    """

    def __init__(self, downstream: SpanExporter, *, policy: ContentPolicy | None = None) -> None:
        self._downstream = downstream
        self._policy = policy or ContentPolicy(content=False)

    def redact(self, span: ReadableSpan) -> ReadableSpan:
        clean = lambda attrs: self._policy.attributes(attrs, content_keys=CONTENT_ATTRS)
        status = Status(span.status.status_code, self._policy.attribute(span.status.description)
                        if self._policy.content else None)
        return ReadableSpan(
            name=span.name, context=span.context, parent=span.parent, resource=span.resource,
            attributes=clean(span.attributes),
            events=[Event(e.name, clean(e.attributes), e.timestamp) for e in span.events],
            links=[Link(link.context, clean(link.attributes)) for link in span.links],
            kind=span.kind, status=status,
            start_time=span.start_time, end_time=span.end_time,
            instrumentation_scope=span.instrumentation_scope,
        )

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self._downstream.export([self.redact(span) for span in spans])

    def shutdown(self) -> None:
        self._downstream.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._downstream.force_flush(timeout_millis)
