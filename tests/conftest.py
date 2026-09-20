"""Keep the unit suite off the developer's real data/ and off the network.

A populated .env makes `create_app()` build a real Runtime; embedded Qdrant
is single-process and the history DB is the demo's, so every test gets its
own throw-away data directory and the deterministic stub agent."""
from dataclasses import dataclass

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tv_avatar.catalog import AvatarProfile, LanguageProfile, SessionPersona
from tv_avatar.config import Settings, get_settings
from tv_avatar.tracing import setup_tracing

#: A persona that never touches avatars.yaml, for tests of the session and
#: pipeline layers that do not care which avatar is speaking.
PERSONA = SessionPersona(
    avatar=AvatarProfile(id="test", name="Test", anam_avatar_id="avatar-1", voice="voice-1"),
    language=LanguageProfile(code="en", name="English", native_name="English"),
)


@pytest.fixture(autouse=True)
def _isolated_runtime_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_IMPL", "stub")
    monkeypatch.setenv("CATALOG_PATH", str(tmp_path / "catalog.parquet"))
    monkeypatch.setenv("QDRANT_PATH", str(tmp_path / "qdrant_db"))
    monkeypatch.setenv("HISTORY_DB_PATH", str(tmp_path / "history.db"))
    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path / "memory"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@dataclass
class Otel:
    exporter: InMemorySpanExporter

    def force_flush(self) -> None:
        from tv_avatar import tracing
        assert tracing._provider is not None
        tracing._provider.force_flush()

    def spans(self) -> dict[str, list]:
        """Finished spans grouped by name, after a flush."""
        self.force_flush()
        out: dict[str, list] = {}
        for s in self.exporter.get_finished_spans():
            out.setdefault(s.name, []).append(s)
        return out


@pytest.fixture(scope="session")
def otel() -> Otel:
    """The one tracer provider of the test process, exporting in memory.

    OTel allows exactly one global provider per process (a second
    ``set_tracer_provider`` is ignored with a warning), so no other test may
    install one — and none constructs a network exporter.
    """
    exporter = InMemorySpanExporter()
    settings = Settings(_env_file=None, slng_api_key="s", anam_api_key="a", nebius_api_key="n")
    assert setup_tracing(settings, exporter=exporter)
    return Otel(exporter)


@pytest.fixture(autouse=True)
def _clear_spans(request):
    # The provider is global, so tests that never ask for `otel` still record
    # spans into it (and the batch processor delivers them late): start every
    # test that asserts on spans from a flushed, clean slate.
    if "otel" in request.fixturenames:
        otel = request.getfixturevalue("otel")
        otel.force_flush()
        otel.exporter.clear()
    yield
