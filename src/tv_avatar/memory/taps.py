"""Frame taps that drive the memory lane from Pipecat frames (D6, D12).

MemoryPrefetchTap: first InterimTranscriptionFrame past the length threshold
per utterance → lane.prefetch (and recs.prefetch_query). Never awaits, always
forwards.

MemoryIngestTap: LLMFullResponseEndFrame → create_task(lane.ingest_turn) with
the last user/assistant pair from the context — skipped when an
InterruptionFrame was seen since the last LLMFullResponseStartFrame, because
a cut-off turn is not a memory.
"""
import asyncio
from typing import Protocol

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from tv_avatar.memory.lane import MemoryLane
from tv_avatar.session.state import SessionState


class _QueryPrefetcher(Protocol):
    def prefetch_query(self, user_id: str, partial_text: str) -> None: ...


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def _fire(coro, what: str) -> asyncio.Task:
    task = asyncio.create_task(coro)

    def _done(t: asyncio.Task) -> None:
        if not t.cancelled() and t.exception() is not None:
            logger.opt(exception=t.exception()).warning("{} task failed", what)

    task.add_done_callback(_done)
    return task


class MemoryPrefetchTap(FrameProcessor):
    def __init__(self, lane: MemoryLane, session: SessionState, *, min_chars: int = 6,
                 recs: _QueryPrefetcher | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._lane = lane
        self._session = session
        self._min_chars = min_chars
        self._recs = recs
        self._fired_this_utterance = False
        self.prefetch_count = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, UserStartedSpeakingFrame):
            self._fired_this_utterance = False
        elif isinstance(frame, InterimTranscriptionFrame) and not self._fired_this_utterance:
            if len(frame.text.strip()) >= self._min_chars:
                self._prefetch(frame.text)
        elif (isinstance(frame, TranscriptionFrame) and not self._fired_this_utterance
              and len(frame.text.strip()) >= self._min_chars):
            # STT without partials: the final is the first text we see — still worth warming.
            self._prefetch(frame.text)
        await self.push_frame(frame, direction)

    def _prefetch(self, text: str) -> None:
        self._fired_this_utterance = True
        self.prefetch_count += 1
        user_id = self._session.user_id or self._session.session_id
        _fire(self._lane.prefetch(user_id, text), "memory prefetch")
        if self._recs is not None:
            self._recs.prefetch_query(user_id, text)
        logger.bind(session_id=self._session.session_id, user_id=user_id).debug(
            "prefetch fired", chars=len(text))


class MemoryIngestTap(FrameProcessor):
    def __init__(self, lane: MemoryLane, session: SessionState, context: LLMContext, **kwargs) -> None:
        super().__init__(**kwargs)
        self._lane = lane
        self._session = session
        self._context = context
        self._interrupted = False
        self.ingest_count = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMFullResponseStartFrame):
            self._interrupted = False
        elif isinstance(frame, InterruptionFrame):
            self._interrupted = True
        elif isinstance(frame, LLMFullResponseEndFrame):
            if self._interrupted:
                logger.bind(session_id=self._session.session_id).debug("ingest skipped: interrupted turn")
            else:
                _fire(self._ingest(), "memory ingest")
        await self.push_frame(frame, direction)

    async def _ingest(self) -> None:
        await asyncio.sleep(0)  # let the assistant aggregator finish writing its message
        user_text, assistant_text = self._last_pair()
        if not user_text:
            return
        self.ingest_count += 1
        user_id = self._session.user_id or self._session.session_id
        await self._lane.ingest_turn(user_id, user_text, assistant_text)

    def _last_pair(self) -> tuple[str, str]:
        user_text, assistant_text = "", ""
        for msg in reversed(self._context.get_messages()):
            role = msg.get("role")
            if role == "assistant" and not assistant_text:
                assistant_text = _text_of(msg.get("content"))
            elif role == "user":
                user_text = _text_of(msg.get("content"))
                break
        return user_text, assistant_text
