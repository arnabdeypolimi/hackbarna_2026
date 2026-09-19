import asyncio

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TextFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.tests.utils import run_test

from tv_avatar.control.bus import CommandBus
from tv_avatar.control.protocol import AgentStatusMsg, CommandMsg, TranscriptMsg
from tv_avatar.pipeline.observers import AgentStatusObserver, TurnLatencyObserver
from tv_avatar.session.state import SessionState


class Passthrough(FrameProcessor):
    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


async def _drain(bus: CommandBus) -> list:
    out = []
    while True:
        try:
            out.append(await asyncio.wait_for(bus.next_outbound(), timeout=0.02))
        except asyncio.TimeoutError:
            return out


async def test_status_observer_emits_states_in_order_and_transcripts():
    bus = CommandBus()
    observer = AgentStatusObserver(bus)
    await run_test(Passthrough(), observers=[observer], frames_to_send=[
        UserStartedSpeakingFrame(),
        TranscriptionFrame(text="play heat", user_id="u1", timestamp="0"),
        UserStoppedSpeakingFrame(),
        LLMFullResponseStartFrame(), LLMTextFrame("On "), LLMTextFrame("it."), LLMFullResponseEndFrame(),
        BotStartedSpeakingFrame(), BotStoppedSpeakingFrame(),
    ], expected_down_frames=None)
    msgs = await _drain(bus)
    states = [m.state for m in msgs if isinstance(m, AgentStatusMsg)]
    assert states == ["listening", "thinking", "speaking", "idle"]
    transcripts = [(m.role, m.text) for m in msgs if isinstance(m, TranscriptMsg)]
    assert transcripts == [("user", "play heat"), ("assistant", "On it.")]


async def test_cancel_turn_never_drops_status_messages():
    bus = CommandBus()
    await bus.dispatch("home", {}, turn_id="t1")
    bus.push_server_message(AgentStatusMsg(state="thinking"))
    assert bus.cancel_turn("t1") == 1
    remaining = await _drain(bus)
    assert len(remaining) == 1 and isinstance(remaining[0], AgentStatusMsg)
    assert not any(isinstance(m, CommandMsg) for m in remaining)


async def test_latency_observer_logs_one_line_per_turn():
    session = SessionState("sess", "tok", 0, user_id="u1")
    observer = TurnLatencyObserver(session)
    from pipecat.frames.frames import LLMContextFrame
    from pipecat.processors.aggregators.llm_context import LLMContext
    await run_test(Passthrough(), observers=[observer], frames_to_send=[
        UserStartedSpeakingFrame(),
        LLMContextFrame(context=LLMContext()),
        LLMTextFrame("hi"),
        BotStoppedSpeakingFrame(),
    ], expected_down_frames=None)
    assert observer.last_marks is not None
    assert "ttft_ms" in observer.last_marks and "turn_total_ms" in observer.last_marks
