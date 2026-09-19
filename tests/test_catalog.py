import hashlib
import math
from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from tv_avatar.recs.catalog import CatalogFilter, CatalogStore, build_catalog

FIXTURE = Path(__file__).parent / "fixtures" / "catalog_sample.csv"
DIMS = 8


def fake_embed(texts: list[str]) -> list[list[float]]:
    """Deterministic token-hash embedding — no network, stable across runs."""
    out = []
    for text in texts:
        vec = [0.0] * DIMS
        for tok in text.lower().split():
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            vec[h % DIMS] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        out.append([v / norm for v in vec])
    return out


@pytest.fixture(scope="module")
def sample_store(tmp_path_factory) -> CatalogStore:
    parquet = tmp_path_factory.mktemp("cat") / "catalog.parquet"
    client = QdrantClient(":memory:")
    build_catalog(FIXTURE, parquet, client=client, limit=150, embed_fn=fake_embed, batch_size=64)
    return CatalogStore(parquet, client=client)


def test_lookup_by_title_id(sample_store):
    item = sample_store.lookup("27205")  # Inception
    assert item is not None
    assert item.name == "Inception"
    assert item.year == 2010
    assert "Science Fiction" in item.genres
    assert sample_store.lookup("nope") is None


def test_search_respects_exclude_filter(sample_store):
    hits = sample_store.search([0.1] * DIMS, CatalogFilter(exclude_ids={"27205"}), limit=5)
    assert len(hits) == 5
    assert all(i.title_id != "27205" for i, _ in hits)


def test_genre_filter(sample_store):
    hits = sample_store.search([0.1] * DIMS, CatalogFilter(genres_any={"Horror"}), limit=10)
    assert hits
    assert all("Horror" in i.genres for i, _ in hits)


def test_year_and_vote_filters(sample_store):
    hits = sample_store.search(
        [0.1] * DIMS, CatalogFilter(year_min=2015, min_vote_count=1000), limit=20
    )
    assert hits
    assert all(i.year >= 2015 and i.vote_count >= 1000 for i, _ in hits)


def test_top_popular_is_sorted(sample_store):
    top = sample_store.top_popular(5)
    pops = [i.popularity for i in top]
    assert pops == sorted(pops, reverse=True)


def test_vectors_round_trip(sample_store):
    vecs = sample_store.vectors(["27205", "missing"])
    assert set(vecs) == {"27205"}
    assert len(vecs["27205"]) == DIMS


def test_build_is_resumable(tmp_path):
    client = QdrantClient(":memory:")
    parquet = tmp_path / "c.parquet"
    calls: list[int] = []

    def counting(texts):
        calls.append(len(texts))
        return fake_embed(texts)

    first = build_catalog(FIXTURE, parquet, client=client, limit=20, embed_fn=counting)
    second = build_catalog(FIXTURE, parquet, client=client, limit=20, embed_fn=counting)
    assert first.embedded == 20 and second.embedded == 0
    assert sum(calls) == 20


def test_index_built_with_another_width_is_rebuilt_not_resumed(tmp_path):
    """Switching embedding provider (1024-dim Nebius -> 384-dim local E5) must
    not leave a resumable-but-unusable index behind."""
    client = QdrantClient(":memory:")
    parquet = tmp_path / "c.parquet"
    build_catalog(FIXTURE, parquet, client=client, limit=20, embed_fn=fake_embed, dims=DIMS)
    store = CatalogStore(parquet, client=client)
    assert store.vector_size() == DIMS

    def wider(texts):
        return [v + [0.0] * DIMS for v in fake_embed(texts)]

    report = build_catalog(FIXTURE, parquet, client=client, limit=20, embed_fn=wider, dims=2 * DIMS)
    assert report.embedded == 20 and report.skipped == 0
    assert CatalogStore(parquet, client=client).vector_size() == 2 * DIMS
