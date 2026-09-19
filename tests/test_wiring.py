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


def test_stub_pipeline_builds_with_phase2_processors(tmp_path):
    """AGENT_IMPL=stub still runs through taps/injector/observers (no keys needed)."""
    from tv_avatar.pipeline.builder import build_pipeline

    class T:
        def input(self): return _Passthrough()
        def output(self): return _Passthrough()
        def event_handler(self, _name):
            return lambda fn: fn

    from pipecat.processors.frame_processor import FrameProcessor

    class _Passthrough(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)

    from tv_avatar.control.bus import CommandBus
    from tv_avatar.session.state import SessionState
    settings = _settings(tmp_path)
    runtime = build_runtime(settings)
    task = build_pipeline(T(), SessionState("s", "t", 0, user_id="u1"), CommandBus(),
                          with_avatar=False, runtime=runtime, settings=settings)
    def flatten(pipeline):
        for p in pipeline.processors:
            if isinstance(p, Pipeline):
                yield from flatten(p)
            else:
                yield p

    names = [type(p).__name__ for p in flatten(task.pipeline)]
    for expected in ("MemoryPrefetchTap", "ScreenContextInjector", "StubLLMService", "MemoryIngestTap"):
        assert expected in names, names
    assert EventKind.PLAY_STARTED  # module import sanity for the recorder wiring


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
