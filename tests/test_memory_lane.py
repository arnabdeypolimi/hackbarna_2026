import asyncio

from tv_avatar.memory.fake import FakeMemoryLane
from tv_avatar.memory.lane import MemoryBlock


async def test_recall_returns_prefetched_when_final_matches_partial():
    lane = FakeMemoryLane({"u1": MemoryBlock.from_lines(["likes sci-fi"], [])})
    await lane.prefetch("u1", "what do I like")
    block = await lane.recall("u1", "what do I like again?")
    assert isinstance(block, MemoryBlock)
    assert "sci-fi" in block.render_for_prompt()
    assert len(lane.searches) == 1  # prefix hit, no second search


async def test_recall_searches_fresh_when_prefix_differs():
    lane = FakeMemoryLane()
    await lane.prefetch("u1", "play something")
    await lane.recall("u1", "what did I watch yesterday")
    assert len(lane.searches) == 2


async def test_recall_waits_for_inflight_prefetch():
    lane = FakeMemoryLane(search_delay_s=0.05)
    task = asyncio.create_task(lane.prefetch("u1", "what do I like"))
    await asyncio.sleep(0.01)
    block = await lane.recall("u1", "what do I like tonight")
    await task
    assert isinstance(block, MemoryBlock)
    assert len(lane.searches) == 1


async def test_ingest_never_raises_into_the_turn():
    lane = FakeMemoryLane(fail_ingest=True)
    await lane.ingest_turn("u1", "I love sci-fi", "noted")
    assert lane.ingests


async def test_empty_block_renders_none_yet():
    assert MemoryBlock().render_for_prompt() == "(none yet)"
    block = MemoryBlock.from_lines(["a"], ["b"])
    assert "Known facts" in block.render_for_prompt() and "Persona" in block.render_for_prompt()


async def test_recall_span_names_its_source(otel):
    """`memory.recall` says why the turn got the block it got (observability plan, Task 4)."""
    lane = FakeMemoryLane({"u1": MemoryBlock.from_lines(["likes sci-fi"], [])})
    await lane.prefetch("u1", "what do I like")
    await lane.recall("u1", "what do I like again?")
    await lane.recall("u1", "what did I watch yesterday")
    spans = otel.spans()
    prefetch, = spans["memory.prefetch"]
    assert prefetch.attributes["langfuse.observation.type"] == "retriever"
    assert prefetch.attributes["tv.memory.chars"] > 0
    hit, search = spans["memory.recall"]
    assert hit.attributes["langfuse.observation.metadata.source"] == "prefetch_hit"
    assert "sci-fi" in hit.attributes["langfuse.observation.output"]
    assert search.attributes["langfuse.observation.metadata.source"] == "search"


async def test_recall_span_records_stale_and_the_wait_for_a_prefetch(otel):
    starved = FakeMemoryLane(search_delay_s=0.05, recall_budget_s=0.02)
    await starved.recall("u1", "first")                      # search over budget, nothing cached
    lane = FakeMemoryLane({"u1": MemoryBlock.from_lines(["likes sci-fi"], [])}, search_delay_s=0.05)
    task = asyncio.create_task(lane.prefetch("u1", "what do I like"))
    await asyncio.sleep(0.01)
    await lane.recall("u1", "what do I like tonight")        # waits on the in-flight prefetch
    await task
    empty, waited = otel.spans()["memory.recall"]
    assert empty.attributes["langfuse.observation.metadata.source"] == "empty"
    assert waited.attributes["tv.memory.waited_ms"] >= 30
    assert waited.attributes["langfuse.observation.metadata.source"] == "prefetch_hit"


async def test_ingest_span_marks_failures_without_raising(otel):
    lane = FakeMemoryLane(fail_ingest=True)
    await lane.ingest_turn("u1", "I love sci-fi", "")
    ingest, = otel.spans()["memory.ingest"]
    assert ingest.attributes["tv.memory.interrupted"] is True
    assert ingest.status.status_code.name == "ERROR"
