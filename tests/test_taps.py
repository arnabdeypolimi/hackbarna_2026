import asyncio

from pipecat.frames.frames import (
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    UserStartedSpeakingFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.tests.utils import SleepFrame, run_test

from tv_avatar.memory.fake import FakeMemoryLane
from tv_avatar.memory.taps import MemoryIngestTap, MemoryPrefetchTap
from tv_avatar.session.state import SessionState


def _session() -> SessionState:
    return SessionState("sess", "tok", 0, user_id="u1")


def _interim(text: str) -> InterimTranscriptionFrame:
    return InterimTranscriptionFrame(text=text, user_id="u1", timestamp="0")


class Recs:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def prefetch_query(self, user_id: str, partial_text: str) -> None:
        self.calls.append(partial_text)


async def test_prefetch_fires_once_per_utterance_and_only_past_threshold():
    lane, recs = FakeMemoryLane(), Recs()
    tap = MemoryPrefetchTap(lane, _session(), min_chars=6, recs=recs)
    # System frames bypass the processor queue, so a SleepFrame separates utterances
    # the way real speech does.
    await run_test(tap, frames_to_send=[
        UserStartedSpeakingFrame(), _interim("what"), _interim("what do I"), _interim("what do I like"),
        SleepFrame(0.02),
        UserStartedSpeakingFrame(), _interim("play the"), _interim("play the second one"),
        SleepFrame(0.02),
    ], expected_down_frames=None)
    assert tap.prefetch_count == 2
    assert [s[1] for s in lane.searches] == ["what do I", "play the"]
    assert recs.calls == ["what do I", "play the"]


async def test_ingest_fires_on_end_frame_with_last_pair():
    lane, ctx = FakeMemoryLane(), LLMContext()
    ctx.add_message({"role": "user", "content": "I love heist movies"})
    ctx.add_message({"role": "assistant", "content": "Noted."})
    tap = MemoryIngestTap(lane, _session(), ctx)
    await run_test(tap, frames_to_send=[LLMFullResponseStartFrame(), LLMFullResponseEndFrame(), SleepFrame(0.02)],
                   expected_down_frames=None)
    await asyncio.sleep(0.01)
    assert lane.ingests == [("u1", "I love heist movies", "Noted.")]


async def test_ingest_skipped_after_interruption():
    lane, ctx = FakeMemoryLane(), LLMContext()
    ctx.add_message({"role": "user", "content": "hello"})
    tap = MemoryIngestTap(lane, _session(), ctx)
    await run_test(tap, frames_to_send=[
        LLMFullResponseStartFrame(), SleepFrame(0.02), InterruptionFrame(), LLMFullResponseEndFrame(), SleepFrame(0.02),
        LLMFullResponseStartFrame(), LLMFullResponseEndFrame(), SleepFrame(0.02),
    ], expected_down_frames=None)
    await asyncio.sleep(0.01)
    assert tap.ingest_count == 1  # only the un-interrupted turn
