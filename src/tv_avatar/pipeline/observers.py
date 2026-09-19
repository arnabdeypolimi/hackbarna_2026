"""Publish agent status and transcripts to the control channel (spec §7).

An observer rather than a processor: it watches every frame without
sitting in the media path, so it cannot add latency to speech.
"""
from collections import deque

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed

from tv_avatar.control.bus import CommandBus
from tv_avatar.control.protocol import AgentStatusMsg, TranscriptMsg


class SessionEventsObserver(BaseObserver):
    """Translate pipeline frames into protocol events on the session's bus."""

    def __init__(self, bus: CommandBus, *, dedupe_window: int = 512) -> None:
        super().__init__()
        self._bus = bus
        # A frame is pushed once per hop; report it once. Bounded so a long
        # session cannot grow the set without limit.
        self._seen: deque[int] = deque(maxlen=dedupe_window)
        self._seen_set: set[int] = set()

    async def on_push_frame(self, data: FramePushed) -> None:
        frame = data.frame
        if frame.id in self._seen_set:
            return
        self._remember(frame.id)
        msg = translate(frame)
        if msg is None:
            return
        if isinstance(msg, TranscriptMsg) and msg.final:
            logger.info("{}: {}", msg.role, msg.text)
        self._bus.publish(msg)

    def _remember(self, frame_id: int) -> None:
        if len(self._seen) == self._seen.maxlen:
            self._seen_set.discard(self._seen[0])
        self._seen.append(frame_id)
        self._seen_set.add(frame_id)


def translate(frame: Frame) -> AgentStatusMsg | TranscriptMsg | None:
    """Pure mapping from a frame to a protocol event, or None if irrelevant."""
    match frame:
        case UserStartedSpeakingFrame():
            return AgentStatusMsg(state="listening")
        case UserStoppedSpeakingFrame():
            return AgentStatusMsg(state="thinking")
        case BotStartedSpeakingFrame():
            return AgentStatusMsg(state="speaking")
        case BotStoppedSpeakingFrame():
            return AgentStatusMsg(state="idle")
        case InterimTranscriptionFrame(text=text):
            return TranscriptMsg(role="user", text=text, final=False)
        case TranscriptionFrame(text=text):
            return TranscriptMsg(role="user", text=text, final=True)
        case TTSTextFrame(text=text):
            return TranscriptMsg(role="assistant", text=text, final=True)
        case _:
            return None
