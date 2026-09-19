"""The real agent: a Pipecat LLMService running SGR over an OpenAI-compatible
endpoint (D8, D12).

Receives LLMContextFrame, streams `say` downstream one complete sentence at a
time as AggregatedTextFrames (see `_speak` for why not raw LLMTextFrames),
dispatches actions in parallel as each array element completes, and runs a
bounded loop of follow-up cycles (AGENT_MAX_CYCLES) when an internal tool
returns data. InterruptionFrame cancels the in-flight completion and the turn's
unsent commands, then keeps flowing so TTS and the avatar stop together.
"""
import asyncio
import contextlib
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
from pipecat.utils.text.simple_text_aggregator import SimpleTextAggregator
from pydantic import BaseModel, ValidationError

from tv_avatar.agent.envelope import REGISTRY, parse_action, turn_plan_schema
from tv_avatar.agent.prompt import (
    build_system_prompt,
    greeting_brief,
    is_greeting,
    render_screen,
    tool_results_message,
    volatile_sections,
)
from tv_avatar.agent.stream_parse import (
    ActionReady,
    Done,
    EnvelopeStreamer,
    IntentReady,
    SayDelta,
    SayDone,
)
from tv_avatar.agent.tools import InternalTools
from tv_avatar.agent.turn import CycleOutcome, ToolResult, TurnContext, TurnMetrics
from tv_avatar.config import Settings
from tv_avatar.control.bus import CommandBus
from tv_avatar.history.store import HistoryStore
from tv_avatar.memory.lane import MemoryBlock, MemoryLane
from tv_avatar.recs.catalog import CatalogStore
from tv_avatar.recs.engine import RecsEngine
from tv_avatar.session.state import SessionState

MAX_TOKENS = 220           # say is 1–2 spoken sentences; actions are small
TEMPERATURE = 0.2
#: Conversation history kept in the prompt (non-system messages). TTFT grows
#: with context on the shared endpoint: 20 messages measured 2.3 s vs ~0.4 s.
MAX_HISTORY_MESSAGES = 10


def render_fallback(results: tuple[ToolResult, ...]) -> tuple[str, list[tuple[str, dict]]]:
    """Spoken answer + TV actions built from tool results without an LLM call."""
    for verb, result in ((r.verb, r.payload) for r in results):
        if verb == "recommend_titles":
            titles = result.get("titles") or []
            if not titles:
                return ("I couldn't find anything matching that right now. Want to try something else?", [])
            names = [f"{t['name']} from {t['year']}" if t.get("year") else t["name"] for t in titles[:3]]
            spoken = names[0] if len(names) == 1 else ", ".join(names[:-1]) + f", or {names[-1]}"
            return (f"How about {spoken}?", [("focus", {"title_id": titles[0]["title_id"]})])
        if verb == "recall_memory":
            memory = (result.get("memory") or "").strip()
            if memory and memory != "(none yet)":
                return (f"Here's what I remember: {memory.splitlines()[-1].lstrip('- ')}", [])
            return ("I don't have that in my memory yet.", [])
    return ("Sorry, that took too long. Could you say it again?", [])


def _last_user_text(messages: list[dict]) -> str:
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


class SGRAgentService(LLMService):
    def __init__(self, settings: Settings, bus: CommandBus, lane: MemoryLane,
                 recs: RecsEngine | None, history: HistoryStore | None, session: SessionState,
                 *, catalog: CatalogStore | None = None, client: Any | None = None,
                 tools: InternalTools | None = None, **kwargs) -> None:
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
        self._tools = tools or InternalTools(recs, lane, catalog, timeout_s=settings.tool_timeout_s)
        self._turn_task: asyncio.Task | None = None
        self._turn_id: str | None = None
        self._interrupted = False
        # What the current turn heard and has said so far — ingested into memory
        # at turn end, or on interruption with the partial reply (D6: the user's
        # words are a memory even when the answer was cut off).
        self._turn_user_text = ""
        self._turn_said: list[str] = []

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
        user_text, said = self._turn_user_text, "".join(self._turn_said)
        self._turn_user_text, self._turn_said = "", []
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
        self._turn_user_text, self._turn_said = user_text, []
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
        log.debug("cycle start", step="cycle", cycle=1, n_messages=len(messages))
        outcome = await self._cycle(messages, ctx, 1, metrics)
        log.debug("cycle envelope", step="cycle", cycle=1, raw=outcome.raw)
        # The SGR loop: a cycle-earning tool result buys one more LLM cycle, up
        # to the cap. Every follow-up cycle must start speaking within its budget
        # or the results are spoken from a template — the loop never outlives it.
        max_cycles = self._cfg.agent_max_cycles
        for cycle in range(2, max_cycles + 1):
            if not outcome.needs_another_cycle:
                break
            feedback = outcome.feedback()
            log.debug("cycle end", step="cycle", cycle=cycle - 1, next_cycle=True,
                      awaited=[r.verb for r in outcome.results], feedback_chars=len(feedback))
            log.debug("tool results fed back", step="feedback", results=feedback)
            messages = messages + [
                {"role": "assistant", "content": outcome.raw},
                {"role": "user", "content": tool_results_message(feedback, final=cycle == max_cycles)},
            ]
            budgeted = await self._cycle_with_budget(messages, ctx, cycle, metrics)
            if budgeted is None:
                metrics.fallback = True
                await self._speak_fallback(outcome.results, ctx)
                break
            outcome = budgeted
        log.debug("cycle end", step="cycle", cycle=metrics.cycles, next_cycle=False,
                  awaited=[r.verb for r in outcome.results])
        await self.push_frame(LLMFullResponseEndFrame())
        await self.stop_processing_metrics()
        metrics.total_ms = ctx.elapsed_ms()
        log.debug("turn close", step="end", **metrics.as_log_fields())
        log.info("turn", **metrics.as_log_fields())
        self._schedule_ingest(interrupted=False)

    async def _cycle_with_budget(self, messages: list[dict], ctx: TurnContext, cycle: int,
                                 metrics: TurnMetrics) -> CycleOutcome | None:
        """Run a follow-up cycle; None if it produced no speech within `cycle_first_byte_s`."""
        budget_s = self._cfg.cycle_first_byte_s
        first_say = asyncio.Event()
        ctx.log.debug("cycle start", step="cycle", cycle=cycle, n_messages=len(messages),
                      budget_ms=round(budget_s * 1000))
        task = asyncio.create_task(self._cycle(messages, ctx, cycle, metrics, first_say=first_say))
        waiter = asyncio.create_task(first_say.wait())
        try:
            done, _ = await asyncio.wait({task, waiter}, timeout=budget_s, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
        if not done:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            ctx.log.warning("cycle over budget; speaking templated answer", step="cycle", cycle=cycle,
                            budget_ms=round(budget_s * 1000))
            return None
        outcome = await task
        ctx.log.debug("cycle envelope", step="cycle", cycle=cycle, raw=outcome.raw)
        return outcome

    async def _speak_fallback(self, results: tuple[ToolResult, ...], ctx: TurnContext) -> None:
        text, actions = render_fallback(results)
        ctx.log.debug("fallback", step="say", text=text, actions=actions)
        # Deliberately not added to _turn_said: a template built from substitute
        # results is not the agent's reply, and the memory profile must not learn
        # the viewer "wanted" whatever the popular channel happened to return.
        await self._speak(text)
        for verb, args in actions:
            await self.dispatch_action(parse_action({"verb": verb, **args}), ctx)

    async def _speak(self, text: str) -> None:
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

    async def _cycle(self, messages: list[dict], ctx: TurnContext, cycle: int, metrics: TurnMetrics,
                     first_say: asyncio.Event | None = None) -> CycleOutcome:
        log = ctx.log
        metrics.cycles = max(metrics.cycles, cycle)  # attempted, even if cancelled over budget
        streamer = EnvelopeStreamer()
        sentences = SimpleTextAggregator()  # same splitter the TTS would use, but we own the flush
        raw: list[str] = []
        said: list[str] = []
        awaited: list[tuple[str, asyncio.Task]] = []
        fire: list[asyncio.Task] = []
        first_say_pending = True
        t_req = time.perf_counter()
        stream = None

        def ms() -> int:
            return round((time.perf_counter() - t_req) * 1000)

        await self.start_ttfb_metrics()
        try:
            stream = await self._client.chat.completions.create(
                model=self._cfg.llm_model, messages=messages, stream=True,
                temperature=TEMPERATURE, max_tokens=MAX_TOKENS,
                response_format={"type": "json_schema", "json_schema": turn_plan_schema()},
                extra_body=self._cfg.llm_extra_body or None,
            )
            async for chunk in stream:
                choice = chunk.choices[0] if chunk.choices else None
                delta = choice.delta.content if choice and choice.delta else None
                if not delta:
                    continue
                raw.append(delta)
                for event in streamer.feed(delta):
                    match event:
                        case IntentReady(intent=intent):
                            metrics.intent = intent
                            log.debug("intent", step="intent", cycle=cycle, intent=intent, ms=ms())
                        case SayDelta(text=text):
                            said.append(text)
                            self._turn_said.append(text)
                            if first_say_pending:
                                first_say_pending = False
                                if first_say is not None:
                                    first_say.set()
                                await self.stop_ttfb_metrics()
                                metrics.mark_once("ttft_ms", ctx.elapsed_ms())
                                log.debug("first say byte", step="say", cycle=cycle, ms=ms())
                            async for sentence in sentences.aggregate(text):
                                log.debug("sentence -> TTS", step="say", cycle=cycle, text=sentence.text, ms=ms())
                                await self._speak(sentence.text)
                        case SayDone():
                            # The say string closed: speak the tail now rather
                            # than after the actions array (or the next cycle).
                            if (tail := await sentences.flush()) is not None:
                                log.debug("sentence -> TTS", step="say", cycle=cycle, text=tail.text, ms=ms())
                                await self._speak(tail.text)
                        case ActionReady(action=raw_action):
                            metrics.n_actions += 1
                            try:
                                action = parse_action(raw_action)
                            except ValidationError as err:
                                log.warning("rejected action", verb=raw_action.get("verb"),
                                            reason=str(err).splitlines()[0])
                                continue
                            verb, spec = str(action.verb), REGISTRY[str(action.verb)]
                            if spec.earns_cycle and cycle >= self._cfg.agent_max_cycles:
                                log.debug("action skipped", step="action", cycle=cycle, verb=verb,
                                          reason="cycle cap")
                                continue  # no open-ended loops on a voice interface
                            log.debug("action ready", step="action", cycle=cycle, verb=verb,
                                      args=action.model_dump(exclude={"verb"}, exclude_none=True),
                                      awaited=spec.awaits_result, ms=ms())
                            task = asyncio.create_task(self.dispatch_action(action, ctx))
                            if spec.awaits_result:
                                awaited.append((verb, task))
                            else:
                                fire.append(task)
                            metrics.mark_once("first_action_ms", ctx.elapsed_ms())
                        case Done():
                            break
                if streamer.finished:
                    break
        finally:
            close = getattr(stream, "close", None)  # also on cancellation during create()
            if close is not None:
                await close()
        if (tail := await sentences.flush()) is not None:  # say never closed: truncated or malformed envelope
            await self._speak(tail.text)
        if first_say_pending:
            await self.stop_ttfb_metrics()
        log.debug("stream done", step="say", cycle=cycle, say="".join(said), chunks=len(raw),
                  awaited=len(awaited), fire_and_forget=len(fire), ms=ms())

        results: list[ToolResult] = []
        for verb, task in awaited:
            try:
                results.append(ToolResult(verb, await task))
            except Exception as err:  # noqa: BLE001
                log.opt(exception=err).debug("awaited action raised", step="action", verb=verb)
                results.append(ToolResult(verb, {"status": "error", "reason": type(err).__name__}))
        for task in fire:
            with contextlib.suppress(Exception):  # failures are logged in dispatch_action
                await task
        return CycleOutcome("".join(raw), tuple(results))

    # --- pieces the tests call directly -------------------------------------

    def build_messages(self, messages: list[dict], memory: MemoryBlock, history_summary: str) -> list[dict]:
        """The agent is the only writer of the system prompt: whatever arrived in
        the system slot is replaced, never inspected (D9 — stamp, never store)."""
        rest = [m for m in messages if m.get("role") != "system"][-MAX_HISTORY_MESSAGES:]
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
