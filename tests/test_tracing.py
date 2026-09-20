"""The tracing bootstrap: off by default, Langfuse OTLP/HTTP contract, redaction,
and session identity riding on every span via baggage."""
from conftest import PERSONA
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tv_avatar.config import Settings
from tv_avatar.session.state import SessionState
from tv_avatar.tracing import (
    ATTR_SESSION_ID,
    CONTENT_ATTRS,
    RedactingSpanExporter,
    build_exporter,
    detached_from_span,
    observation,
    session_scope,
    tracer,
    tracing_wanted,
)


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, slng_api_key="s", anam_api_key="a", nebius_api_key="n", **kw)


def _session() -> SessionState:
    return SessionState("sess_1", "tok", 0, persona=PERSONA, user_id="u1")


def test_tracing_off_by_default_and_when_unconfigured():
    assert tracing_wanted(_settings()) is False
    assert tracing_wanted(_settings(tracing_enabled=True)) is False  # enabled but no sink
    assert tracing_wanted(_settings(langfuse_public_key="pk", langfuse_secret_key="sk")) is False


def test_langfuse_keys_build_basic_auth_http_exporter():
    s = _settings(tracing_enabled=True, langfuse_public_key="pk-lf-x", langfuse_secret_key="sk-lf-y")
    assert tracing_wanted(s)
    exp = build_exporter(s)
    assert exp._endpoint == "https://cloud.langfuse.com/api/public/otel/v1/traces"
    assert exp._headers["Authorization"] == "Basic cGstbGYteDpzay1sZi15"
    assert exp._headers["x-langfuse-ingestion-version"] == "4"


def test_langfuse_base_url_alias_and_trailing_slash(monkeypatch):
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://us.cloud.langfuse.com/")
    s = _settings(tracing_enabled=True, langfuse_public_key="pk", langfuse_secret_key="sk")
    assert build_exporter(s)._endpoint == "https://us.cloud.langfuse.com/api/public/otel/v1/traces"


def test_endpoint_override_wins_over_langfuse_keys():
    s = _settings(tracing_enabled=True, langfuse_public_key="pk", langfuse_secret_key="sk",
                  otel_exporter_otlp_endpoint="http://localhost:4318",
                  otel_exporter_otlp_headers="a=b,c=d=e")
    exp = build_exporter(s)
    assert exp._endpoint == "http://localhost:4318/v1/traces"
    assert "Authorization" not in exp._headers
    assert exp._headers == {"a": "b", "c": "d=e"}
    s2 = _settings(tracing_enabled=True, otel_exporter_otlp_endpoint="http://c:4318/v1/traces")
    assert build_exporter(s2)._endpoint == "http://c:4318/v1/traces"


def test_spans_reach_injected_exporter(otel):
    with tracer().start_as_current_span("probe") as span:
        span.set_attribute(ATTR_SESSION_ID, "sess_1")
    assert "probe" in otel.spans()


def test_redacting_exporter_strips_content_attrs():
    sink = InMemorySpanExporter()
    provider = TracerProvider()  # local, never installed globally
    provider.add_span_processor(SimpleSpanProcessor(RedactingSpanExporter(sink)))
    with provider.get_tracer("t").start_as_current_span("llm") as span:
        span.set_attributes({"messages": "[...]", "langfuse.observation.input": "hi",
                             "langfuse.trace.output": "bye",
                             "langfuse.observation.metadata.intent": "control"})
    with provider.get_tracer("t").start_as_current_span("plain") as span:
        span.set_attribute("tv.turn.ttft_ms", 3)
    llm, plain = sink.get_finished_spans()
    assert not CONTENT_ATTRS & set(llm.attributes)
    assert llm.attributes["langfuse.observation.metadata.intent"] == "control"
    assert llm.name == "llm" and llm.context.span_id and llm.end_time
    assert plain.attributes["tv.turn.ttft_ms"] == 3


def test_redaction_covers_events_status_and_links():
    from opentelemetry.trace import Link, Status, StatusCode

    sink = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(RedactingSpanExporter(sink)))
    with provider.get_tracer("t").start_as_current_span("parent") as parent:
        link = Link(parent.get_span_context(), {"text": "private-link"})
        with provider.get_tracer("t").start_as_current_span("child", links=[link]) as span:
            span.add_event("exception", {"exception.type": "ValueError",
                                         "exception.message": "private-error",
                                         "exception.stacktrace": "private-stack"})
            span.add_event("speech", {"text": "private-speech", "index": 1})
            span.set_status(Status(StatusCode.ERROR, "private-status"))
    child = sink.get_finished_spans()[0]
    assert "private" not in str(child.attributes)
    assert "private" not in str([e.attributes for e in child.events])
    assert "private" not in str([link.attributes for link in child.links])
    assert child.status.description is None
    assert child.status.status_code is StatusCode.ERROR
    assert child.events[0].attributes["exception.type"] == "ValueError"
    provider.shutdown()


def test_content_policy_bounds_and_masks_payloads():
    import json

    from tv_avatar.telemetry import ContentPolicy

    policy = ContentPolicy(content=True, max_chars=128, secrets=("test-credential",))
    encoded = policy.encode({"text": "hello test-credential", "api_key": "hidden"})
    assert "test-credential" not in encoded and "hidden" not in encoded
    truncated = json.loads(policy.encode({"text": "x" * 1000}))
    assert truncated["truncated"] is True
    assert len(truncated["preview"]) <= 128
    assert ContentPolicy(content=False).encode({"say": "private"}) is None


def test_console_and_remote_exporters_share_the_content_policy(monkeypatch):
    from tv_avatar import tracing

    remote, console = InMemorySpanExporter(), InMemorySpanExporter()
    monkeypatch.setattr(tracing, "_provider", None)
    monkeypatch.setattr(tracing.trace, "set_tracer_provider", lambda provider: None)
    monkeypatch.setattr(tracing, "ConsoleSpanExporter", lambda: console)
    settings = _settings(trace_content=False, otel_console_export=True)
    assert tracing.setup_tracing(settings, exporter=remote)
    provider = tracing._provider
    try:
        with provider.get_tracer("t").start_as_current_span("probe") as span:
            span.set_attribute("langfuse.observation.output", "private-output")
            span.set_attribute("tv.speech.submitted", "private-speech")
            span.add_event("exception", {"exception.message": "private-error"})
        provider.force_flush()
        for sink in (remote, console):
            exported, = sink.get_finished_spans()
            assert "private" not in exported.to_json()
    finally:
        tracing.shutdown_tracing()


def test_exporter_failure_does_not_escape_into_application():
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

    class BrokenExporter(SpanExporter):
        def export(self, spans):
            raise ConnectionError("export offline")

    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(RedactingSpanExporter(BrokenExporter())))
    with provider.get_tracer("t").start_as_current_span("work"):
        pass
    provider.force_flush()
    provider.shutdown()


async def test_synthetic_smoke_covers_agent_without_network(otel, monkeypatch):
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    from tools.langfuse_smoke import synthetic_turn

    session_id, trace_id = await synthetic_turn(_settings())
    spans = otel.spans()
    root, = spans["telemetry.synthetic_turn"]
    assert format(root.context.trace_id, "032x") == trace_id
    assert len(spans["agent.cycle"]) == len(spans["agent.plan"]) == 2
    assert all(s.context.trace_id == root.context.trace_id for s in spans["agent.cycle"])
    assert root.attributes["langfuse.session.id"] == session_id
    assert len(spans["tv.command_result"]) == 3
    sink = InMemorySpanExporter()
    RedactingSpanExporter(sink).export([span for group in spans.values() for span in group])
    exported = " ".join(span.to_json() for span in sink.get_finished_spans())
    assert "Find a space film" not in exported, [
        key for span in sink.get_finished_spans() for key, value in span.attributes.items()
        if "Find a space film" in str(value)]
    assert "I found Moon" not in exported


def test_session_scope_puts_identity_on_every_span(otel):
    with (session_scope(_session(), _settings(), half_duplex=True),
          observation("outer", type="span"),
          observation("inner", type="tool", **{"tv.action.ms": None, "tv.x": 1})):
        pass
    spans = otel.spans()
    for s in spans["outer"] + spans["inner"]:
        assert s.attributes["langfuse.session.id"] == "sess_1"
        assert s.attributes["langfuse.user.id"] == "u1"
        assert s.attributes["langfuse.trace.name"] == "tv-avatar-session"
        assert s.attributes["langfuse.trace.metadata.avatar_id"] == "test"
        assert s.attributes["langfuse.trace.metadata.half_duplex"] == "true"
    inner, = spans["inner"]
    assert inner.attributes["langfuse.observation.type"] == "tool"
    assert inner.attributes["tv.x"] == 1 and "tv.action.ms" not in inner.attributes
    assert inner.parent.span_id == spans["outer"][0].context.span_id


def test_spans_outside_session_scope_carry_no_identity(otel):
    with observation("orphan", type="span"):
        pass
    assert "langfuse.session.id" not in otel.spans()["orphan"][0].attributes


def test_detached_context_is_root_but_keeps_baggage(otel):
    with (session_scope(_session(), _settings()), observation("turn", type="span"),
          tracer().start_as_current_span("fold", context=detached_from_span())):
        pass
    fold, = otel.spans()["fold"]
    assert fold.parent is None
    assert fold.attributes["langfuse.session.id"] == "sess_1"


def test_an_app_booted_under_an_existing_provider_does_not_own_it(otel):
    """create_app() with TRACING_ENABLED=true in a traced process (this test suite,
    or an embedding host) must not shut the shared provider down on exit."""
    from tv_avatar import tracing
    on = _settings(tracing_enabled=True, langfuse_public_key="pk", langfuse_secret_key="sk")
    assert tracing.setup_tracing(on) is False           # already installed by the fixture
    assert tracing._provider is not None


def test_redaction_runs_on_the_export_thread_not_in_span_end():
    """Tracing stays off the media path (CLAUDE.md): the per-span rebuild must
    happen where the batch processor exports, never inside ``span.end()``."""
    import threading

    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    seen: list[str] = []
    exported = threading.Event()

    class Recording(InMemorySpanExporter):
        def export(self, spans):
            seen.append(threading.current_thread().name)
            result = super().export(spans)
            exported.set()
            return result

    sink = Recording()
    provider = TracerProvider()
    # A short schedule so the batch worker exports by itself: force_flush()
    # would drain on the calling thread and prove nothing.
    provider.add_span_processor(BatchSpanProcessor(RedactingSpanExporter(sink), schedule_delay_millis=10))
    with provider.get_tracer("t").start_as_current_span("llm") as span:
        span.set_attribute("langfuse.observation.input", "private")
    assert exported.wait(5)
    provider.shutdown()
    span_out, = sink.get_finished_spans()
    assert "langfuse.observation.input" not in span_out.attributes
    assert seen[0] != threading.main_thread().name
