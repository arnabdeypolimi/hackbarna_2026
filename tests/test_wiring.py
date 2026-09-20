import asyncio
import json

from fastapi.testclient import TestClient
from pipecat.pipeline.pipeline import Pipeline

from tv_avatar.app import create_app
from tv_avatar.config import Settings
from tv_avatar.control.protocol import PROTOCOL_VERSION
from tv_avatar.history.store import EventKind
from tv_avatar.runtime import build_runtime


def _settings(tmp_path) -> Settings:
    return Settings(nebius_api_key="-", slng_api_key="-", anam_api_key="-", anam_avatar_id="-", _env_file=None,
                    history_db_path=str(tmp_path / "h.db"), catalog_path=str(tmp_path / "none.parquet"),
                    qdrant_path=str(tmp_path / "none_qdrant"))


def test_session_round_trips_user_id_and_defaults_to_anon():
    with TestClient(create_app()) as c:
        body = c.post("/sessions", json={"user_id": "couch_1"}).json()
        assert body["user_id"] == "couch_1"
        assert body["offer_url"].endswith("/offer")
        anon = c.post("/sessions").json()
        assert anon["user_id"].startswith("anon_sess_")


def test_session_rejects_a_user_id_that_could_walk_the_memory_root():
    with TestClient(create_app()) as c:
        assert c.post("/sessions", json={"user_id": "../../etc"}).status_code == 422
        assert c.post("/sessions", json={"user_id": "a" * 65}).status_code == 422
        assert c.post("/sessions", json={"user_id": "usr_mfdzvhxjxrg6"}).status_code == 200


def test_catalog_sample_is_empty_without_a_built_catalog():
    with TestClient(create_app()) as c:
        assert c.get("/catalog/sample").json() == {"titles": []}


def test_runtime_picks_the_embedder_and_refuses_a_mismatched_index(tmp_path):
    """EMBEDDING_PROVIDER decides the query embedder; an index built with another
    width disables recs up front instead of raising inside a live turn."""
    from pathlib import Path

    from tv_avatar.recs.catalog import build_catalog
    from tv_avatar.recs.embedder import LocalE5Embedder, OpenAIEmbedder
    from tv_avatar.runtime import build_embedder

    assert isinstance(build_embedder(_settings(tmp_path)), LocalE5Embedder)
    assert isinstance(build_embedder(_settings(tmp_path).model_copy(update={"embedding_provider": "nebius"})),
                      OpenAIEmbedder)

    fixture = Path(__file__).parent / "fixtures" / "catalog_sample.csv"
    parquet, qdrant = tmp_path / "c.parquet", tmp_path / "q"
    build_catalog(fixture, parquet, qdrant, limit=10, embed_fn=lambda ts: [[1.0] * 8 for _ in ts], dims=8)
    settings = _settings(tmp_path).model_copy(update={"catalog_path": str(parquet), "qdrant_path": str(qdrant)})
    runtime = build_runtime(settings)
    assert runtime.catalog is not None and runtime.recs is None  # 8-dim index vs 384-dim local E5
    asyncio.run(runtime.close())


def test_screen_transition_over_the_socket_records_history(tmp_path):
    runtime = build_runtime(_settings(tmp_path))
    app = create_app(runtime=runtime)
    with TestClient(app) as c:
        body = c.post("/sessions", json={"user_id": "u1"}).json()
        url = f"/sessions/{body['session_id']}/control?token={body['control_token']}"
        with c.websocket_connect(url) as ws:
            ws.receive_json()  # agent_status idle
            for state in ("stopped", "playing"):
                ws.send_text(json.dumps({
                    "v": PROTOCOL_VERSION, "type": "screen_state",
                    "state": {"view": "player" if state == "playing" else "grid", "tiles": [],
                              "playback": {"state": state, "title_id": "27205" if state == "playing" else None,
                                           "position_s": 0.0}},
                }))
            ws.send_text(json.dumps({"v": PROTOCOL_VERSION, "type": "user_event",
                                     "event": "remote_press", "detail": {"key": "ok"}}))
            ws.send_text(json.dumps({"v": 99}))
            assert ws.receive_json()["code"] == "unsupported_version"  # forces the reader to have drained
            c.portal.call(asyncio.sleep, 0.1)  # let the fire-and-forget recorder tasks land
        watched = c.portal.call(runtime.history.watched_ids, "u1")
        assert watched == {"27205"}


def test_offer_rejects_bad_token():
    with TestClient(create_app()) as c:
        sid = c.post("/sessions").json()["session_id"]
        r = c.post(f"/sessions/{sid}/offer?token=bad", json={"sdp": "v=0", "type": "offer"})
        assert r.status_code == 401


def _build_task(settings):
    from pipecat.processors.frame_processor import FrameProcessor

    from tv_avatar.control.bus import CommandBus
    from tv_avatar.pipeline.builder import build_pipeline
    from tv_avatar.session.state import SessionState

    class _Passthrough(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)

    class T:
        def input(self): return _Passthrough()
        def output(self): return _Passthrough()
        def event_handler(self, _name):
            return lambda fn: fn

    runtime = build_runtime(settings)
    return build_pipeline(T(), SessionState("s", "t", 0, user_id="u1"), CommandBus(),
                          with_avatar=False, runtime=runtime, settings=settings)


def _processor_names(settings) -> list[str]:
    task = _build_task(settings)

    def flatten(pipeline):
        for p in pipeline.processors:
            if isinstance(p, Pipeline):
                yield from flatten(p)
            else:
                yield p

    return [type(p).__name__ for p in flatten(task.pipeline)]


def test_stub_pipeline_builds_with_phase2_processors(tmp_path):
    """AGENT_IMPL=stub still runs through taps/injector/observers (no keys needed)."""
    names = _processor_names(_settings(tmp_path))
    for expected in ("MemoryPrefetchTap", "ScreenContextInjector", "StubLLMService", "MemoryIngestTap"):
        assert expected in names, names
    assert EventKind.PLAY_STARTED  # module import sanity for the recorder wiring


def test_sgr_pipeline_has_no_injector(tmp_path):
    """The SGR agent writes its own system prompt; a second writer upstream
    would stamp Recent activity twice (as it did) and fetch history twice."""
    names = _processor_names(_settings(tmp_path).model_copy(update={"agent_impl": "sgr"}))
    assert "SGRAgentService" in names and "ScreenContextInjector" not in names, names
    assert "MemoryIngestTap" not in names  # the agent ingests at turn end itself


def test_echo_guard_is_off_by_default(tmp_path):
    """It dropped "no die hard" as echo of "No Hard Feelings" (2026-09-20); the
    whole guard — filter and the observer that feeds it — is now opt-in."""
    from tv_avatar.pipeline.echo import BotSpeechObserver

    task = _build_task(_settings(tmp_path))
    assert "EchoTranscriptFilter" not in _processor_names(_settings(tmp_path))
    assert not any(isinstance(o, BotSpeechObserver) for o in task._observer._observers)


def test_echo_guard_is_wired_when_enabled(tmp_path):
    from tv_avatar.pipeline.echo import BotSpeechObserver

    on = _settings(tmp_path).model_copy(update={"echo_filter": True})
    names = _processor_names(on)
    assert names.index("EchoTranscriptFilter") < names.index("MemoryPrefetchTap"), names
    assert any(isinstance(o, BotSpeechObserver) for o in _build_task(on)._observer._observers)


def test_task_carries_tracing_flags_from_settings(tmp_path):
    """Pipecat's own tracing is switched per task (D14/D17); the private names
    are the PipelineTask attributes on 1.11.0 — a rename is the signal we want."""
    on = _settings(tmp_path).model_copy(update={
        "tracing_enabled": True, "langfuse_public_key": "pk", "langfuse_secret_key": "sk"})
    task = _build_task(on)
    assert task._enable_tracing is True
    assert task._conversation_id == "s"
    assert task._additional_span_attributes["langfuse.session.id"] == "s"
    assert task._additional_span_attributes["langfuse.user.id"] == "u1"
    assert _build_task(_settings(tmp_path))._enable_tracing is False


def test_new_offer_for_same_user_stops_that_users_other_pipelines(tmp_path, monkeypatch):
    from tv_avatar import app as app_module
    monkeypatch.setattr(app_module, "_settings_available", lambda: False)  # 503 right after the replace
    runtime = build_runtime(_settings(tmp_path))
    app = create_app(runtime=runtime)
    stopped: list[str] = []
    app.state.manager.stop_pipeline = stopped.append  # type: ignore[method-assign]
    with TestClient(app) as c:
        old = c.post("/sessions", json={"user_id": "couch"}).json()
        new = c.post("/sessions", json={"user_id": "couch"}).json()
        other = c.post("/sessions", json={"user_id": "someone_else"}).json()
        r = c.post(f"/sessions/{new['session_id']}/offer?token={new['control_token']}",
                   json={"sdp": "v=0", "type": "offer"})
        assert r.status_code == 503
    assert stopped == [old["session_id"]]
    assert other["session_id"] not in stopped
