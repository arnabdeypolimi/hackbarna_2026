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


def test_tracing_dependencies_import():
    """OTel stack (observability plan D13): HTTP exporter only — Langfuse rejects gRPC."""
    from importlib.util import find_spec

    from opentelemetry import trace  # noqa: F401
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter,  # noqa: F401
    )
    from opentelemetry.processor.baggage import BaggageSpanProcessor  # noqa: F401
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,  # noqa: F401
    )
    from pipecat.utils.tracing.setup import is_tracing_available
    assert is_tracing_available()
    assert find_spec("opentelemetry.exporter.otlp.proto.grpc") is None


def test_pipecat_stack_unchanged():
    # Phase-1 pins must survive the new dependency tree.
    assert version("pipecat-anam") == "0.2.0a6"
    assert version("pipecat-slng") == "0.5.2"
