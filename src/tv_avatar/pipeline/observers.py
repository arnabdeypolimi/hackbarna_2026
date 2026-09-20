"""Pipecat observers: publish session events to the control channel (spec §7)
and derive per-turn latency from frames.

Observers rather than processors: they watch every frame without sitting in
the media path, so they cannot add latency to speech. A frame is pushed once
per hop; each observer reports it once.
"""
import time
from collections import deque
from collections.abc import Callable

from loguru import logger
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext
from pipecat.frames.frames import (
    AggregatedTextFrame,
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    ErrorFrame,
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
from tv_avatar.tracing import (
    ATTR_LATENCY_PREFIX,
    ATTR_OBS_LEVEL,
    ATTR_OBS_TYPE,
    EVENT_ERROR,
    LEVEL_ERROR,
    META_N_ERRORS,
    OBS_TYPE_EVENT,
    tracer,
)

#: Where the observer finds Pipecat's current turn span (D16): evaluated at emit
#: time because the observer is built before the task that owns the turn tracker.
TurnContextFn = Callable[[], SpanContext | None]


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

    def __init__(self, session: SessionState, *, turn_context: TurnContextFn | None = None) -> None:
        super().__init__()
        self._session = session
        self._turn_context = turn_context
        self._reset()

    def _reset(self) -> None:
        self._t_interim: float | None = None
        self._t_context: float | None = None
        self._t_first_text: float | None = None
        self._t_interrupt: float | None = None
        self._metrics: dict[str, float] = {}
        self._errors: list[tuple[str, bool]] = []
        self._emitted = False
        self.last_marks: dict | None = None

    def _turn_span_context(self):
        """OTel context of Pipecat's turn span, or None (tracing off, before turn 1)."""
        ctx = self._turn_context() if self._turn_context is not None else None
        return trace.set_span_in_context(NonRecordingSpan(ctx)) if ctx is not None else None

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
        elif isinstance(frame, (LLMTextFrame, AggregatedTextFrame)) and self._t_first_text is None:
            # LLMTextFrame from the chat LLM; the SGR agent hands TTS whole sentences.
            self._t_first_text = now
        elif isinstance(frame, InterruptionFrame):
            self._t_interrupt = now
        elif isinstance(frame, ErrorFrame):
            # Errors like "SLNG TTS context abandoned" or "Anam avatar does not
            # exist" otherwise live only in the console: they become events on
            # the turn's latency span (below) and a filterable count.
            self._errors.append((frame.error, frame.fatal))
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
        if self._t_interim and self._t_context and self._t_context > self._t_interim:
            marks["interim_to_context_ms"] = round((self._t_context - self._t_interim) * 1000)
        if self._t_context and self._t_first_text:
            marks["ttft_ms"] = round((self._t_first_text - self._t_context) * 1000)
        if self._t_context:
            marks["turn_total_ms"] = round((now - self._t_context) * 1000)
        if self._errors:
            marks["n_errors"] = len(self._errors)
        self.last_marks = marks
        logger.bind(session_id=self._session.session_id, user_id=self._session.user_id,
                    turn_id=self._session.current_turn_id or "-").info("turn latency", **marks)
        # The same marks as a zero-duration `event` under Pipecat's turn span (D16):
        # one producer, two sinks. Pipecat's own span object is not reachable
        # from here (only its context), so the errors ride on this span instead.
        if (ctx := self._turn_span_context()) is None:
            return
        attrs = {ATTR_OBS_TYPE: OBS_TYPE_EVENT, META_N_ERRORS: len(self._errors),
                 **{ATTR_LATENCY_PREFIX + k: v for k, v in marks.items() if k != "n_errors"}}
        if any(fatal for _, fatal in self._errors):
            attrs[ATTR_OBS_LEVEL] = LEVEL_ERROR
        span = tracer().start_span("turn.latency", context=ctx, attributes=attrs)
        for message, fatal in self._errors:
            span.add_event(EVENT_ERROR, {"message": message, "fatal": fatal})
        span.end()
