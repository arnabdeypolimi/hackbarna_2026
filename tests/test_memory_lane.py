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
