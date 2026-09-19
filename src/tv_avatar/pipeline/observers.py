"""Pipecat observers: publish session events to the control channel (spec §7)
and derive per-turn latency from frames.

Observers rather than processors: they watch every frame without sitting in
the media path, so they cannot add latency to speech. A frame is pushed once
per hop; each observer reports it once.
"""
import time
from collections import deque

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMContextFrame,
    LLMTextFrame,
    MetricsFrame,
    TranscriptionFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import ProcessingMetricsData, TTFBMetricsData
from pipecat.observers.base_observer import BaseObserver, FramePushed

from tv_avatar.control.bus import CommandBus
from tv_avatar.control.protocol import AgentStatusMsg, TranscriptMsg
from tv_avatar.session.state import SessionState


class _DedupObserver(BaseObserver):
    def __init__(self, *, dedupe_window: int = 512) -> None:
        super().__init__()
        # Bounded so a long session cannot grow the set without limit.
        self._seen: deque[int] = deque(maxlen=dedupe_window)
        self._seen_set: set[int] = set()

    def _first_time(self, frame: Frame) -> bool:
        if frame.id in self._seen_set:
            return False
        if len(self._seen) == self._seen.maxlen:
            self._seen_set.discard(self._seen[0])
        self._seen.append(frame.id)
        self._seen_set.add(frame.id)
        return True


class SessionEventsObserver(_DedupObserver):
    """Translate pipeline frames into protocol events on the session's bus."""

    def __init__(self, bus: CommandBus, *, dedupe_window: int = 512) -> None:
        super().__init__(dedupe_window=dedupe_window)
        self._bus = bus

    async def on_push_frame(self, data: FramePushed) -> None:
        if not self._first_time(data.frame):
            return
        msg = translate(data.frame)
        if msg is None:
            return
        if isinstance(msg, TranscriptMsg) and msg.final:
            logger.info("{}: {}", msg.role, msg.text)
        self._bus.publish(msg)


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


class TurnLatencyObserver(_DedupObserver):
    """One structured log line per turn — M4's data source (phase 2, Task 8).

    Marks: first interim → LLMContextFrame, LLMContextFrame → first
    LLMTextFrame (TTFT), InterruptionFrame → bot stopped, plus Pipecat's own
    TTFB/processing metrics. Emitted on BotStoppedSpeakingFrame.
    """

    def __init__(self, session: SessionState) -> None:
        super().__init__()
        self._session = session
        self._reset()

    def _reset(self) -> None:
        self._t_interim: float | None = None
        self._t_context: float | None = None
        self._t_first_text: float | None = None
        self._t_interrupt: float | None = None
        self._metrics: dict[str, float] = {}
        self._emitted = False
        self.last_marks: dict | None = None

    async def on_push_frame(self, data: FramePushed) -> None:
        if not self._first_time(data.frame):
            return
        frame, now = data.frame, time.perf_counter()
        if isinstance(frame, UserStartedSpeakingFrame):
            self._reset()
        elif isinstance(frame, InterimTranscriptionFrame) and self._t_interim is None:
            self._t_interim = now
        elif isinstance(frame, LLMContextFrame) and self._t_context is None:
            # The assistant aggregator re-pushes a context frame after the reply;
            # only the first one per turn marks the start of inference.
            self._t_context = now
        elif isinstance(frame, LLMTextFrame) and self._t_first_text is None:
            self._t_first_text = now
        elif isinstance(frame, InterruptionFrame):
            self._t_interrupt = now
        elif isinstance(frame, MetricsFrame):
            for m in frame.data:
                if isinstance(m, TTFBMetricsData):
                    self._metrics["pipecat_ttfb_ms"] = round(m.value * 1000)
                elif isinstance(m, ProcessingMetricsData):
                    self._metrics["pipecat_processing_ms"] = round(m.value * 1000)
        elif isinstance(frame, BotStoppedSpeakingFrame):
            if self._t_interrupt is not None and self._t_first_text is None:
                # The bot stopped because the user barged in: this is the stop
                # latency of the interrupted utterance, not the end of a turn.
                logger.bind(session_id=self._session.session_id, user_id=self._session.user_id,
                            turn_id=self._session.current_turn_id or "-").info(
                    "interrupt stop", interrupt_stop_ms=round((now - self._t_interrupt) * 1000))
                self._t_interrupt = None
            elif not self._emitted:
                self._emitted = True
                self._emit(now)

    def _emit(self, now: float) -> None:
        marks: dict = dict(self._metrics)
        if self._t_interim and self._t_context:
            marks["interim_to_context_ms"] = round((self._t_context - self._t_interim) * 1000)
        if self._t_context and self._t_first_text:
            marks["ttft_ms"] = round((self._t_first_text - self._t_context) * 1000)
        if self._t_context:
            marks["turn_total_ms"] = round((now - self._t_context) * 1000)
        self.last_marks = marks
        logger.bind(session_id=self._session.session_id, user_id=self._session.user_id,
                    turn_id=self._session.current_turn_id or "-").info("turn latency", **marks)
