"""Phase-2 dependency proof — run before any phase-2 code exists."""
from importlib.metadata import version


def test_new_dependencies_import():
    import aiosqlite  # noqa: F401
    import openai  # noqa: F401
    import polars  # noqa: F401
    import pyarrow  # noqa: F401
    from qdrant_client import QdrantClient  # noqa: F401


def test_sentence_transformers_is_a_direct_dependency():
    """The local E5 recs embedder loads it itself (it used to arrive via VoiceMem)."""
    import sentence_transformers  # noqa: F401
    assert version("sentence-transformers")


def test_pipecat_stack_unchanged():
    # Phase-1 pins must survive the new dependency tree.
    assert version("pipecat-anam") == "0.2.0a6"
    assert version("pipecat-slng") == "0.5.2"
