from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import FramePushed
from pipecat.processors.frame_processor import FrameDirection

from tv_avatar.control.bus import CommandBus
from tv_avatar.pipeline.observers import SessionEventsObserver, translate


def test_status_frames_map_to_agent_status():
    assert translate(UserStartedSpeakingFrame()).state == "listening"
    assert translate(UserStoppedSpeakingFrame()).state == "thinking"
    assert translate(BotStartedSpeakingFrame()).state == "speaking"
    assert translate(BotStoppedSpeakingFrame()).state == "idle"


def test_transcription_frames_map_to_transcripts():
    partial = translate(InterimTranscriptionFrame(text="hel", user_id="u", timestamp="t"))
    final = translate(TranscriptionFrame(text="hello", user_id="u", timestamp="t"))
    spoken = translate(TTSTextFrame(text="Hi there.", aggregated_by="sentence"))
    assert (partial.role, partial.final) == ("user", False)
    assert (final.role, final.final, final.text) == ("user", True, "hello")
    assert (spoken.role, spoken.text) == ("assistant", "Hi there.")


def test_unrelated_frames_are_ignored():
    from pipecat.frames.frames import StartFrame
    assert translate(StartFrame()) is None


async def test_observer_reports_each_frame_once_across_hops():
    """A frame is pushed once per pipeline hop; the client must see one event."""
    bus = CommandBus()
    observer = SessionEventsObserver(bus)
    frame = UserStartedSpeakingFrame()
    for _hop in range(4):
        await observer.on_push_frame(FramePushed(
            source=None, destination=None, frame=frame,
            direction=FrameDirection.DOWNSTREAM, timestamp=0,
        ))
    first = await bus.next_outbound()
    assert first.type == "agent_status" and first.state == "listening"
    assert not bus._outbound


async def test_bus_events_survive_turn_cancellation():
    bus = CommandBus()
    await bus.dispatch("home", {}, turn_id="turn_1")
    bus.publish(translate(BotStoppedSpeakingFrame()))
    assert bus.cancel_turn("turn_1") == 1
    assert (await bus.next_outbound()).type == "agent_status"
