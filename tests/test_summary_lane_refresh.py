"""The every-N mid-session fold counts from the last fold, whichever caller ran it."""
import asyncio

from test_summary_lane import FakeLLM

from tv_avatar.config import Settings
from tv_avatar.memory.summary_lane import SummaryMemoryLane


async def test_session_end_fold_resets_the_refresh_counter(tmp_path):
    """Session A leaves the counter one short; session B's first turn must not
    trigger a fold over a one-turn transcript."""
    llm = FakeLLM()
    settings = Settings(nebius_api_key="x", slng_api_key="-", anam_api_key="-", anam_avatar_id="-",
                        _env_file=None, memory_root=str(tmp_path / "mem"), memory_refresh_every_turns=3)
    lane = SummaryMemoryLane(settings, client=llm)
    await lane.ingest_turn("u1", "something slow", "Let me look.")
    await lane.ingest_turn("u1", "no, sadder", "Got it.")
    await lane.finish_session("u1")                      # the runner, at session end
    assert len(llm.calls) == 1
    await lane.ingest_turn("u1", "hello again", "Welcome back.")
    await asyncio.sleep(0.05)                            # a refresh would be fire-and-forget
    assert len(llm.calls) == 1
    assert lane._since_refresh["u1"] == 1
