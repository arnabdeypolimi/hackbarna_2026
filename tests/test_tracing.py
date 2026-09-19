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
    RedactingSpanProcessor,
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


def test_redacting_processor_strips_content_attrs():
    sink = InMemorySpanExporter()
    provider = TracerProvider()  # local, never installed globally
    provider.add_span_processor(RedactingSpanProcessor(SimpleSpanProcessor(sink)))
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
