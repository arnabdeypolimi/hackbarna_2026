import asyncio
import os
import time

import pytest

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


@pytest.mark.skipif(not os.environ.get("VOICEMEM_LIVE"), reason="live VoiceMem (set VOICEMEM_LIVE=1)")
async def test_prefetch_is_not_serialised_behind_a_slow_ingest(tmp_path):
    """mem0 holds locks in places; a multi-second ingest must not block the
    next turn's prefetch. Fires both, asserts prefetch returns inside budget."""
    from tv_avatar.config import Settings
    from tv_avatar.memory.voicemem_lane import VoiceMemLane

    settings = Settings(slng_api_key="-", anam_api_key="-", anam_avatar_id="-",
                        memory_root=str(tmp_path / "vm"))
    lane = VoiceMemLane(settings)
    await lane.warmup()
    await lane.recall("u1", "hello")  # open the space before timing anything
    ingest = asyncio.create_task(lane.ingest_turn("u1", "I hate horror films", "noted"))
    await asyncio.sleep(0.05)
    t0 = time.perf_counter()
    await asyncio.wait_for(lane.prefetch("u1", "what should I"), timeout=1.0)
    assert time.perf_counter() - t0 < 0.7
    await ingest
