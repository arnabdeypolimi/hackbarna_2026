from pathlib import Path

import pytest
from pipecat.frames.frames import LLMContextFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.tests.utils import run_test
from qdrant_client import QdrantClient

from tv_avatar.agent.injector import ScreenContextInjector, render_screen
from tv_avatar.control.protocol import Playback, ScreenState, Tile
from tv_avatar.history.store import Event, EventKind, HistoryStore
from tv_avatar.recs.catalog import CatalogStore, build_catalog
from tv_avatar.recs.embedder import hash_embed
from tv_avatar.session.state import SessionState

FIXTURE = Path(__file__).parent / "fixtures" / "catalog_sample.csv"


@pytest.fixture(scope="module")
def catalog(tmp_path_factory) -> CatalogStore:
    parquet = tmp_path_factory.mktemp("inj") / "c.parquet"
    client = QdrantClient(":memory:")
    build_catalog(FIXTURE, parquet, client=client, limit=50, embed_fn=lambda ts: [hash_embed(t, 8) for t in ts])
    return CatalogStore(parquet, client=client)


def _session() -> SessionState:
    s = SessionState("sess", "tok", 0, user_id="u1")
    s.update_screen(ScreenState(
        view="grid", rail_id="r1", focus_index=1,
        tiles=[Tile(title_id="tt_x", name="Unknown Title", position=0),
               Tile(title_id="27205", name="Inception", position=1)],
        playback=Playback(state="stopped"),
    ))
    return s


def test_render_enriches_known_tiles_and_keeps_unknown_names(catalog):
    text = render_screen(_session(), catalog)
    assert "Inception (2010) — Action, Science Fiction, Adventure (id=27205) <- focused" in text
    assert "[0] Unknown Title (id=tt_x)" in text


async def test_injector_rewrites_system_message_with_screen_and_history(catalog, tmp_path):
    history = HistoryStore(str(tmp_path / "h.db"))
    await history.record(Event(user_id="u1", kind=EventKind.PLAY_STARTED, title_id="27205"))
    ctx = LLMContext()
    ctx.add_message({"role": "system", "content": "stale"})
    ctx.add_message({"role": "user", "content": "play the focused one"})
    injector = ScreenContextInjector(_session(), catalog=catalog, history=history)
    try:
        # A cold pipecat start can miss run_test's 1 s default on a busy box.
        await run_test(injector, frames_to_send=[LLMContextFrame(context=ctx)],
                       expected_down_frames=[LLMContextFrame], start_timeout=5.0)
        messages = ctx.get_messages()
        assert [m["role"] for m in messages] == ["system", "user"]
        system = messages[0]["content"]
        assert "stale" not in system
        assert "# Capabilities" in system and "# Screen" in system
        assert "Inception (2010)" in system and "<- focused" in system
        assert "Recently watched: Inception (2010)" in system
    finally:
        await history.close()  # an open aiosqlite thread keeps a failed run from exiting
