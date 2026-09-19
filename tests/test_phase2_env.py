"""Phase-2 dependency proof — run before any phase-2 code exists."""
from importlib.metadata import version


def test_new_dependencies_import():
    import openai  # noqa: F401
    import polars  # noqa: F401
    import pyarrow  # noqa: F401
    import aiosqlite  # noqa: F401
    from qdrant_client import QdrantClient  # noqa: F401


def test_voicemem_imports_and_constructs_in_text_config():
    """Import alone must not load local models (they are lazy), and a
    VoiceMem instance must be constructible without downloading anything."""
    import voicemem
    assert hasattr(voicemem, "VoiceMem")


def test_pipecat_stack_unchanged():
    # Phase-1 pins must survive the new dependency tree.
    assert version("pipecat-anam") == "0.2.0a6"
    assert version("pipecat-slng") == "0.5.2"
