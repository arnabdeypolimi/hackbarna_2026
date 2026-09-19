"""One local E5 for the process: `intfloat/multilingual-e5-small`.

Catalog rows are embedded as passages by tools/build_catalog.py and queries
as queries by the recommendation engine, through this module, so a single
SentenceTransformer is loaded and forward passes never overlap (concurrent
torch inference crashed the process once, 2026-09-19).

Measured on the dev Mac, CPU pinned to two threads: ~15 ms per short query,
~440 catalog passages/s. The cloud embedder this replaces on the read path
needed 200–500 ms and routinely blew the 240 ms tool budget.
"""
import threading
from typing import Any, Literal

from loguru import logger

E5_MODEL = "intfloat/multilingual-e5-small"
#: multilingual-e5-small output width — the catalog index is built with it.
E5_DIM = 384
E5_THREADS = 2  # measured faster than all-threads on the dev Mac

Kind = Literal["query", "passage"]

_lock = threading.Lock()
_torch_pinned = False
_model: Any | None = None
_model_override: Any | None = None


def pin_torch_threads() -> None:
    """Once per process, before any inference — changing it mid-inference is fatal."""
    global _torch_pinned
    if _torch_pinned:
        return
    try:
        import torch
        torch.set_num_threads(E5_THREADS)
        _torch_pinned = True
    except Exception as err:  # noqa: BLE001 — log, don't fail: inference still works unpinned
        logger.warning("could not pin torch threads: {}", type(err).__name__)


def model() -> Any:
    """The shared SentenceTransformer (loads on first use, ~6 s; HF cache afterwards)."""
    global _model
    if _model_override is not None:
        return _model_override
    if _model is None:
        with _lock:
            if _model is None:
                pin_torch_threads()
                from sentence_transformers import SentenceTransformer
                try:
                    # Cache first: with the model already downloaded, the default
                    # path still HEADs huggingface.co and, on flaky venue Wi-Fi,
                    # retries for ~30 s before falling back to the same cache.
                    _model = SentenceTransformer(E5_MODEL, local_files_only=True)
                except Exception:  # noqa: BLE001 — not cached yet: one online download
                    logger.info("E5 not in the local cache; downloading {}", E5_MODEL)
                    _model = SentenceTransformer(E5_MODEL)
    return _model


def encode(texts: list[str], *, kind: Kind) -> list[list[float]]:
    """Normalised vectors, one per text. Blocking: call off the event loop.

    E5's ``query:`` / ``passage:`` prefixes are part of the model contract,
    not decoration — a query embedded as a passage lands in the wrong region.
    """
    if not texts:
        return []
    prefixed = [f"{kind}: {t}" for t in texts]
    m = model()
    with _lock:
        vectors = m.encode(prefixed, normalize_embeddings=True)
    return [list(map(float, v)) for v in vectors]


def set_model_for_tests(fake: Any | None) -> None:
    global _model_override
    _model_override = fake
