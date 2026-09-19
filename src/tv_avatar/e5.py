"""One local E5 for the whole process.

`intfloat/multilingual-e5-small` serves two lanes: VoiceMem's memory/anchor
vectors and the recommendation engine's query + catalog vectors. Both go
through here so a single SentenceTransformer is loaded (VoiceMem's
`shared_e5()` singleton) and forward passes never overlap — concurrent torch
inference plus a thread-count change crashed the process (2026-09-19).

Measured on the dev Mac, CPU pinned to two threads: ~15 ms per short query,
~440 catalog passages/s. The cloud embedder this replaces on the read path
needed 200–500 ms and routinely blew the 240 ms tool budget.
"""
import threading
from typing import Any, Literal

from loguru import logger

#: multilingual-e5-small output width — VoiceMem anchors and the catalog index alike.
E5_DIM = 384
E5_THREADS = 2  # measured faster than all-threads on the dev Mac

Kind = Literal["query", "passage"]

#: Re-entrant: VoiceMem's `model.encode` is wrapped in this same lock (see
#: memory/voicemem_lane.py), and `encode()` below calls it while holding it.
_lock = threading.RLock()
_torch_pinned = False
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
    except Exception as err:  # noqa: BLE001 — torch is a voicemem dependency; log, don't fail
        logger.warning("could not pin torch threads: {}", type(err).__name__)


def model() -> Any:
    """The shared SentenceTransformer (loads on first use, ~6 s)."""
    if _model_override is not None:
        return _model_override
    pin_torch_threads()
    from voicemem.leftbrain.local_e5_embedder import shared_e5
    return shared_e5()


def encode(texts: list[str], *, kind: Kind) -> list[list[float]]:
    """Normalised vectors, one per text. Blocking: call off the event loop.

    E5's ``query:`` / ``passage:`` prefixes are part of the model contract,
    not decoration — a query embedded as a passage lands in the wrong region.
    """
    if not texts:
        return []
    prefixed = [f"{kind}: {t}" for t in texts]
    with _lock:
        vectors = model().encode(prefixed, normalize_embeddings=True)
    return [list(map(float, v)) for v in vectors]


def lock() -> threading.RLock:
    """For callers that hold the model directly (VoiceMem's own encode path)."""
    return _lock


def set_model_for_tests(fake: Any | None) -> None:
    global _model_override
    _model_override = fake
