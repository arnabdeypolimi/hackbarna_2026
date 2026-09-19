"""The shared local E5: prefixes, locking, and the recs-side embedder — all
against an injected fake model, so no 6 s SentenceTransformer load in CI."""
import threading
import time

import numpy as np
import pytest

from tv_avatar import e5
from tv_avatar.recs.embedder import LocalE5Embedder


class FakeModel:
    """Records what it was asked to encode; optional delay to expose overlap."""

    def __init__(self, delay_s: float = 0.0) -> None:
        self.calls: list[list[str]] = []
        self.active = 0
        self.max_active = 0
        self.delay_s = delay_s

    def encode(self, texts, normalize_embeddings=False, **_):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            self.calls.append(list(texts))
            if self.delay_s:
                time.sleep(self.delay_s)
            return np.ones((len(texts), e5.E5_DIM), dtype=np.float32) / np.sqrt(e5.E5_DIM)
        finally:
            self.active -= 1


@pytest.fixture
def fake_model():
    model = FakeModel()
    e5.set_model_for_tests(model)
    yield model
    e5.set_model_for_tests(None)


def test_encode_applies_e5_prefixes_and_returns_plain_floats(fake_model):
    q = e5.encode(["Office", "rainy autumn"], kind="query")
    p = e5.encode(["Barbie (2023). Genres: Comedy."], kind="passage")
    assert fake_model.calls == [["query: Office", "query: rainy autumn"],
                                ["passage: Barbie (2023). Genres: Comedy."]]
    assert len(q) == 2 and len(q[0]) == e5.E5_DIM and isinstance(q[0][0], float)
    assert abs(sum(v * v for v in p[0]) - 1.0) < 1e-5
    assert e5.encode([], kind="query") == []


def test_encode_serialises_concurrent_callers(fake_model):
    """Overlapping torch forward passes crashed the process; the lock forbids them."""
    fake_model.delay_s = 0.02
    threads = [threading.Thread(target=e5.encode, args=([f"t{i}"],), kwargs={"kind": "query"})
               for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert fake_model.max_active == 1 and len(fake_model.calls) == 6


async def test_local_e5_embedder_embeds_queries_off_the_loop(fake_model):
    vectors = await LocalE5Embedder().embed(["something like Sicario"])
    assert fake_model.calls == [["query: something like Sicario"]]
    assert len(vectors) == 1 and len(vectors[0]) == LocalE5Embedder.dims == e5.E5_DIM
