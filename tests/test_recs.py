import asyncio
from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from tv_avatar.history.store import Event, EventKind, HistoryStore
from tv_avatar.recs.catalog import CatalogFilter, CatalogStore, build_catalog
from tv_avatar.recs.embedder import FakeEmbedder, hash_embed
from tv_avatar.recs.engine import RecsContext, RecsEngine

FIXTURE = Path(__file__).parent / "fixtures" / "catalog_sample.csv"
DIMS = 8


@pytest.fixture(scope="module")
def catalog(tmp_path_factory) -> CatalogStore:
    parquet = tmp_path_factory.mktemp("recs") / "c.parquet"
    client = QdrantClient(":memory:")
    build_catalog(FIXTURE, parquet, client=client, limit=150,
                  embed_fn=lambda ts: [hash_embed(t, DIMS) for t in ts])
    return CatalogStore(parquet, client=client)


@pytest.fixture
async def history(tmp_path) -> HistoryStore:
    store = HistoryStore(str(tmp_path / "h.db"))
    yield store
    await store.close()


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder(dims=DIMS)


@pytest.fixture
def engine(catalog, history, embedder) -> RecsEngine:
    return RecsEngine(catalog, history, embedder, tool_timeout_s=0.4)


@pytest.fixture
async def u1_with_history(history):
    await history.record(Event(user_id="u1", kind=EventKind.PLAY_STARTED, title_id="27205"))
    await history.record(Event(user_id="u1", kind=EventKind.PLAY_COMPLETED, title_id="27205"))


async def test_watched_titles_never_recommended(engine, u1_with_history):
    recs = await engine.recommend(RecsContext(user_id="u1", query_text="heist dream"))
    assert recs
    assert "27205" not in {r.title_id for r in recs}


async def test_cold_start_falls_back_to_popular(engine):
    recs = await engine.recommend(RecsContext(user_id="new_user", limit=5))
    assert len(recs) == 5
    assert all(r.reasons == ["popular"] for r in recs)


async def test_recommend_returns_under_timeout(engine, u1_with_history):
    await asyncio.wait_for(
        engine.recommend(RecsContext(user_id="u1", query_text="space")), timeout=0.4)


async def test_slow_embedder_degrades_to_taste_not_match(catalog, history, u1_with_history):
    engine = RecsEngine(catalog, history, FakeEmbedder(DIMS, delay_s=0.3), tool_timeout_s=0.05)
    recs = await engine.recommend(RecsContext(user_id="u1", query_text="space"))
    assert recs
    assert all("match" not in r.reasons for r in recs)
    assert any("for you" in r.reasons for r in recs)


async def test_prefetch_makes_query_a_cache_hit(engine, embedder):
    engine.prefetch_query("u1", "something like a heist movie")
    await asyncio.sleep(0)  # let the task run
    recs = await engine.recommend(RecsContext(user_id="u1", query_text="Something like a heist movie please"))
    assert any("match" in r.reasons for r in recs)
    assert len(embedder.calls) == 1  # prefix hit; no second embed call


async def test_genre_constraint_is_hard(engine):
    ctx = RecsContext(user_id="u9", query_text="scary", constraints=CatalogFilter(genres_any={"Horror"}))
    recs = await engine.recommend(ctx)
    assert recs
    for r in recs:
        assert "Horror" in engine._catalog.lookup(r.title_id).genres


async def test_similar_excludes_anchor(engine):
    recs = await engine.similar("27205", RecsContext(user_id="u9", limit=5))
    assert recs and "27205" not in {r.title_id for r in recs}
    assert all("like Inception" in r.reasons for r in recs)
