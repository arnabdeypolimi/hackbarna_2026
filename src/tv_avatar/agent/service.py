"""The real agent: a Pipecat LLMService running SGR over an OpenAI-compatible
endpoint (D8, D12).

This module is the Pipecat glue. It receives LLMContextFrame, assembles the
prompt (system prompt + memory + history, greeting brief), brackets the turn in
LLMFullResponseStart/EndFrames, and hands the cycle loop to `TurnRunner`
(loop.py), for which it is the `TurnHost`: `speak` pushes one complete
sentence to TTS as an AggregatedTextFrame, `dispatch_action` routes internal
tools in-process and TV verbs over the bus. InterruptionFrame cancels the
in-flight turn and its unsent commands, then keeps flowing so TTS and the
avatar stop together; the partial reply is still ingested into memory.
"""
import asyncio
import time
from typing import Any

from loguru import logger
from openai import AsyncOpenAI
from pipecat.frames.frames import (
    AggregatedTextFrame,
    Frame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import LLMService, LLMSettings
from pipecat.utils.text.base_text_aggregator import AggregationType
from pydantic import BaseModel

from tv_avatar.agent.envelope import REGISTRY
from tv_avatar.agent.loop import MAX_TOKENS, TEMPERATURE, TurnRunner
from tv_avatar.agent.prompt import (
    build_system_prompt,
    greeting_brief,
    is_greeting,
    render_screen,
    volatile_sections,
)
from tv_avatar.agent.tools import InternalTools
from tv_avatar.agent.turn import TurnContext, TurnMetrics, TurnTrace
from tv_avatar.config import Settings
from tv_avatar.control.bus import CommandBus
from tv_avatar.history.recorder import HistoryRecorder
from tv_avatar.history.store import HistoryStore
from tv_avatar.memory.lane import MemoryBlock, MemoryLane
from tv_avatar.recs.catalog import CatalogStore
from tv_avatar.recs.engine import RecsEngine
from tv_avatar.session.state import SessionState

#: Conversation history kept in the prompt (non-system messages). TTFT grows
#: with context on the shared endpoint: 20 messages measured 2.3 s vs ~0.4 s.
MAX_HISTORY_MESSAGES = 10


def _text_of(msg: dict) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def _last_user_text(messages: list[dict]) -> str:
    return next((_text_of(m) for m in reversed(messages) if m.get("role") == "user"), "")


class SGRAgentService(LLMService):
    def __init__(self, settings: Settings, bus: CommandBus, lane: MemoryLane,
                 recs: RecsEngine | None, history: HistoryStore | None, session: SessionState,
                 *, catalog: CatalogStore | None = None, client: Any | None = None,
                 tools: InternalTools | None = None, recorder: HistoryRecorder | None = None,
                 **kwargs) -> None:
        kwargs.setdefault("settings", LLMSettings(
            model=settings.llm_model, temperature=TEMPERATURE, max_tokens=MAX_TOKENS,
            system_instruction=None, top_p=None, top_k=None, frequency_penalty=None,
            presence_penalty=None, seed=None, filter_incomplete_user_turns=None,
            user_turn_completion_config=None,
        ))
        super().__init__(**kwargs)
        self._cfg = settings
        self._bus = bus
        self._lane = lane
        self._history = history
        self._catalog = catalog
        self._session = session
        self._client = client or AsyncOpenAI(api_key=settings.nebius_api_key, base_url=settings.nebius_base_url)
        self._tools = tools or InternalTools(recs, catalog, recorder=recorder,
                                             timeout_s=settings.tool_timeout_s)
        self._recorder = recorder
        self._runner = TurnRunner(self._client, settings, self)
        self._turn_task: asyncio.Task | None = None
        self._turn_id: str | None = None
        self._interrupted = False
        # What the current turn heard, said and pointed at — ingested into memory
        # at turn end, or on interruption with the partial reply (D6: the user's
        # words are a memory even when the answer was cut off).
        self._trace = TurnTrace()

    def can_generate_metrics(self) -> bool:
        return True

    # --- Pipecat entry point -------------------------------------------------

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame):
            await self._run_turn(frame.context)
        elif isinstance(frame, InterruptionFrame):
            await self._cancel_turn()
            await self.push_frame(frame, direction)
        else:
            await self.push_frame(frame, direction)

    async def _cancel_turn(self) -> None:
        self._interrupted = True
        if self._turn_task is not None and not self._turn_task.done():
            self._turn_task.cancel()
            self._schedule_ingest(interrupted=True)
        if self._turn_id is not None:
            dropped = self._bus.cancel_turn(self._turn_id)
            logger.bind(session_id=self._session.session_id, turn_id=self._turn_id).info(
                "turn interrupted", dropped_commands=dropped)

    def _schedule_ingest(self, *, interrupted: bool) -> None:
        """Off the turn: never awaited by the pipeline."""
        trace, self._trace = self._trace, TurnTrace()
        user_text, said = trace.user_text, trace.spoken()
        if not user_text.strip() or is_greeting(user_text):
            return
        user_id = self._session.user_id or self._session.session_id
        logger.bind(session_id=self._session.session_id, user_id=user_id, turn_id=self._turn_id).debug(
            "memory ingest scheduled", step="ingest", interrupted=interrupted, said_chars=len(said))
        asyncio.create_task(self._lane.ingest_turn(user_id, user_text, said))

    # --- the turn -----------------------------------------------------------

    async def _run_turn(self, context: LLMContext) -> None:
        turn_id = self._session.new_turn()
        self._turn_id = turn_id
        self._interrupted = False
        self._turn_task = asyncio.create_task(self._turn(context, turn_id))
        try:
            await self._turn_task
        except asyncio.CancelledError:
            # Pipecat cancels the processor's frame task *before* it delivers the
            # InterruptionFrame; without this the inner turn kept streaming.
            if not self._turn_task.done():
                self._turn_task.cancel()
            self._schedule_ingest(interrupted=True)
            if not self._interrupted:
                raise
        finally:
            self._turn_task = None

    async def _turn(self, context: LLMContext, turn_id: str) -> None:
        user_id = self._session.user_id or self._session.session_id
        ctx = TurnContext(turn_id, user_id, time.perf_counter(),
                          logger.bind(session_id=self._session.session_id, user_id=user_id, turn_id=turn_id))
        log, metrics = ctx.log, TurnMetrics()

        await self.start_processing_metrics()
        messages = list(context.get_messages())
        user_text = _last_user_text(messages)
        self._trace = TurnTrace(user_text=user_text)
        log.debug("turn open", step="start", user_text=user_text, history_msgs=len(messages),
                  screen=self._session.screen is not None)

        memory = await self._lane.recall(user_id, user_text)
        history_text = (await self._history.render_for_prompt(user_id, self._catalog)
                        if self._history is not None else "Recently watched: (none yet)")
        metrics.recall_ms = ctx.elapsed_ms()
        log.debug("turn context", step="recall", memory_empty=memory.empty, memory_stale=memory.stale,
                  memory_tokens=memory.token_est, ms=metrics.recall_ms)
        log.debug("turn memory block", step="recall", memory=memory.render_for_prompt(), history=history_text)

        messages = self.build_messages(messages, memory, history_text)
        if is_greeting(user_text):
            messages[-1] = {"role": "user", "content": greeting_brief(
                history_text, memory.render_for_prompt(), self._session.persona.language)}
        log.debug("turn prompt", step="prompt", n_messages=len(messages),
                  system_chars=len(messages[0]["content"]), model=self._cfg.llm_model)
        log.trace("turn system prompt", step="prompt", system=messages[0]["content"])

        await self.push_frame(LLMFullResponseStartFrame())
        fallback_text = await self._runner.run(messages, ctx, metrics, self._trace)
        await self.push_frame(LLMFullResponseEndFrame())
        await self.stop_processing_metrics()
        self._record_offered(ctx, fallback_text)
        metrics.total_ms = ctx.elapsed_ms()
        log.debug("turn close", step="end", **metrics.as_log_fields())
        log.info("turn", **metrics.as_log_fields())
        self._schedule_ingest(interrupted=False)

    def _record_offered(self, ctx: TurnContext, fallback_text: str) -> None:
        """Viewing log: the recommendation candidates the agent actually named or
        focused this turn — not everything the tool returned. Off the turn."""
        offered = self._trace.offered_ids(fallback_text)
        if offered and self._recorder is not None:
            ctx.log.debug("recommendations offered", step="history", title_ids=offered)
            self._recorder.spawn(self._recorder.on_rec_shown(ctx.user_id, offered))

    async def speak(self, text: str) -> None:
        """Hand one complete sentence to TTS.

        Not an LLMTextFrame: the TTS service's own sentence aggregator releases
        a sentence only once it sees the *next* non-space character after the
        punctuation. A cycle's last sentence has no next character until the
        next cycle streams — so "Let me find something." sat unspoken while the
        tool ran and then came out glued to the answer ("...something.Here are
        ...") as one utterance, which a single barge-in dropped whole.
        """
        text = text.strip()
        if text:
            await self.push_frame(AggregatedTextFrame(text, AggregationType.SENTENCE))

    # --- TurnHost + pieces the tests call directly ---------------------------

    def build_messages(self, messages: list[dict], memory: MemoryBlock, history_summary: str) -> list[dict]:
        """The agent is the only writer of the system prompt: whatever arrived in
        the system slot is replaced, never inspected (D9 — stamp, never store).

        The greeting stage direction is dropped from the history once the turn
        has moved on: it sits in the context as a *user* message, and a small
        model that still sees "Greet them…" answers "hello" — and "yes" — with
        the greeting again (seen live). The greeting turn itself keeps it as
        its last message, where `_turn` swaps in the brief."""
        rest = [m for m in messages if m.get("role") != "system"]
        rest = [m for i, m in enumerate(rest)
                if i == len(rest) - 1 or not is_greeting(_text_of(m))][-MAX_HISTORY_MESSAGES:]
        system = build_system_prompt(self._session.persona.language) + "\n\n" + volatile_sections(
            render_screen(self._session, self._catalog), memory.render_for_prompt(), history_summary)
        return [{"role": "system", "content": system}, *rest]

    async def dispatch_action(self, action: BaseModel, ctx: TurnContext) -> dict:
        """Route one parsed action: internal tools in-process, TV verbs over the bus."""
        log, turn_id, user_id = ctx.log, ctx.turn_id, ctx.user_id
        t0 = time.perf_counter()
        verb = str(action.verb)
        args = action.model_dump(exclude={"verb"}, exclude_none=True)
        if REGISTRY[verb].kind == "internal":
            log.debug("dispatch internal", step="dispatch", verb=verb, args=args)
            result = await self._tools.run(action, user_id)
            ms = round((time.perf_counter() - t0) * 1000)
            log.debug("internal tool result", step="dispatch", verb=verb, result=result, ms=ms)
            log.info("internal tool", verb=verb, status=result.get("status", "ok"),
                     n=len(result.get("titles", [])), ms=ms)
            return result
        log.debug("dispatch tv", step="dispatch", verb=verb, args=args)
        try:
            result = await self._bus.dispatch(verb, args, turn_id=turn_id)
        except ValueError as err:
            log.warning("rejected action", verb=verb, reason=str(err))
            return {"status": "invalid", "reason": str(err)}
        log.info("tv command", verb=verb, status=result.get("status"),
                 ms=round((time.perf_counter() - t0) * 1000))
        return result
