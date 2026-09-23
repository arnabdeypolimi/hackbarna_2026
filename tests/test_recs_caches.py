"""The recs engine's two caches are bounded and go stale on purpose."""
import time

import pytest

from tv_avatar.recs import engine as engine_mod
from tv_avatar.recs.embedder import FakeEmbedder
from tv_avatar.recs.engine import RecsEngine


class _Catalog:
    def top_popular(self, limit=10, exclude=None):
        return []

    def vectors(self, ids):
        return {i: [1.0, 0.0] for i in ids}


class _History:
    def __init__(self) -> None:
        self.engaged: list[str] = []
        self.reads = 0

    async def engaged_ids(self, user_id):
        self.reads += 1
        return list(self.engaged)


@pytest.fixture
def engine() -> tuple[RecsEngine, _History]:
    history = _History()
    return RecsEngine(_Catalog(), history, FakeEmbedder(dims=2)), history


async def test_query_cache_sweeps_expired_entries_once_it_grows(engine, monkeypatch):
    eng, _ = engine
    monkeypatch.setattr(engine_mod, "QUERY_CACHE_SWEEP_AT", 4)
    for i in range(4):
        await eng._embed_and_cache(("u", f"q{i}"), f"q{i}")
    assert len(eng._query_cache) == 4
    stale = time.monotonic() - engine_mod.QUERY_CACHE_TTL_S - 1
    eng._query_cache = {k: (stale, v) for k, (_t, v) in eng._query_cache.items()}
    await eng._embed_and_cache(("u", "fresh"), "fresh")
    assert list(eng._query_cache) == [("u", "fresh")]


async def test_taste_is_recomputed_after_its_ttl_and_on_invalidate(engine, monkeypatch):
    eng, history = engine
    assert await eng._taste_vector("u") is None and history.reads == 1
    history.engaged = ["1"]
    assert await eng._taste_vector("u") is None and history.reads == 1  # still cached
    eng.invalidate_taste("u")
    assert await eng._taste_vector("u") == [1.0, 0.0] and history.reads == 2
    monkeypatch.setattr(engine_mod, "TASTE_TTL_S", 0.0)
    await eng._taste_vector("u")
    assert history.reads == 3
