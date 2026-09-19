"""Pipecat observers: derive TV-facing status and per-turn latency from frames.

Nobody tracks state by hand — every status transition and every latency mark
is a frame observation. Observers see each push between each pair of
processors, so frames are de-duplicated by id.
"""
import time

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    MetricsFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import ProcessingMetricsData, TTFBMetricsData
from pipecat.observers.base_observer import BaseObserver, FramePushed

from tv_avatar.control.bus import CommandBus
from tv_avatar.control.protocol import AgentStatusMsg, TranscriptMsg
from tv_avatar.session.state import SessionState

_STATUS_BY_FRAME = (
    (UserStartedSpeakingFrame, "listening"),
    (UserStoppedSpeakingFrame, "thinking"),
    (LLMFullResponseStartFrame, "thinking"),
    (BotStartedSpeakingFrame, "speaking"),
    (BotStoppedSpeakingFrame, "idle"),
)


class _Dedup(BaseObserver):
    def __init__(self) -> None:
        super().__init__()
        self._seen: set[int] = set()

    def _first_time(self, data: FramePushed) -> bool:
        if data.frame.id in self._seen:
            return False
        self._seen.add(data.frame.id)
        if len(self._seen) > 5000:
            self._seen = set(list(self._seen)[-1000:])
        return True


class AgentStatusObserver(_Dedup):
    def __init__(self, bus: CommandBus, *, transcripts: bool = True) -> None:
        super().__init__()
        self._bus = bus
        self._transcripts = transcripts
        self._say: list[str] = []
        self.state = "idle"

    async def on_push_frame(self, data: FramePushed) -> None:
        if not self._first_time(data):
            return
        frame = data.frame
        for kind, state in _STATUS_BY_FRAME:
            if isinstance(frame, kind):
                self._set(state)
                break
        if not self._transcripts:
            return
        if isinstance(frame, TranscriptionFrame):
            self._bus.push_server_message(TranscriptMsg(role="user", text=frame.text, final=True))
        elif isinstance(frame, LLMFullResponseStartFrame):
            self._say = []
        elif isinstance(frame, LLMTextFrame):
            self._say.append(frame.text)
        elif isinstance(frame, LLMFullResponseEndFrame) and self._say:
            self._bus.push_server_message(TranscriptMsg(role="assistant", text="".join(self._say), final=True))
            self._say = []

    def _set(self, state: str) -> None:
        if state == self.state:
            return
        self.state = state
        self._bus.push_server_message(AgentStatusMsg(state=state))


class TurnLatencyObserver(_Dedup):
    """One structured log line per turn — M4's data source."""

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
        self.last_marks: dict | None = None

    async def on_push_frame(self, data: FramePushed) -> None:
        if not self._first_time(data):
            return
        frame, now = data.frame, time.perf_counter()
        if isinstance(frame, UserStartedSpeakingFrame):
            self._reset()
        elif isinstance(frame, InterimTranscriptionFrame) and self._t_interim is None:
            self._t_interim = now
        elif isinstance(frame, LLMContextFrame):
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
            self._emit(now)

    def _emit(self, now: float) -> None:
        marks: dict = dict(self._metrics)
        if self._t_interim and self._t_context:
            marks["interim_to_context_ms"] = round((self._t_context - self._t_interim) * 1000)
        if self._t_context and self._t_first_text:
            marks["ttft_ms"] = round((self._t_first_text - self._t_context) * 1000)
        if self._t_interrupt:
            marks["interrupt_stop_ms"] = round((now - self._t_interrupt) * 1000)
        if self._t_context:
            marks["turn_total_ms"] = round((now - self._t_context) * 1000)
        self.last_marks = marks
        logger.bind(session_id=self._session.session_id, user_id=self._session.user_id,
                    turn_id=self._session.current_turn_id or "-").info("turn latency", **marks)
