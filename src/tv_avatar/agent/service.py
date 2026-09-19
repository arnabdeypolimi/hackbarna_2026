"""The real agent: a Pipecat LLMService running SGR over an OpenAI-compatible
endpoint (D8, D12).

Receives LLMContextFrame, streams `say` downstream as LLMTextFrames (so
SlngTTSService speaks it exactly like any other LLM's output), dispatches
actions in parallel as each array element completes, and runs a bounded
second cycle when an internal tool returns data. InterruptionFrame cancels
the in-flight completion and the turn's unsent commands, then keeps flowing
so TTS and the avatar stop together.
"""
import asyncio
import contextlib
import json
import time
from typing import Any

from loguru import logger
from openai import AsyncOpenAI
from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import LLMService, LLMSettings

from tv_avatar.agent.envelope import AWAITED_VERBS, INTERNAL_AWAIT, turn_plan_schema
from tv_avatar.agent.prompt import build_system_prompt, volatile_sections
from tv_avatar.agent.stream_parse import (
    ActionReady,
    Done,
    EnvelopeStreamer,
    IntentReady,
    SayDelta,
)
from tv_avatar.agent.tools import InternalTools
from tv_avatar.config import Settings
from tv_avatar.control.bus import CommandBus
from tv_avatar.history.store import HistoryStore
from tv_avatar.memory.lane import MemoryBlock, MemoryLane
from tv_avatar.recs.catalog import CatalogStore
from tv_avatar.recs.engine import RecsEngine
from tv_avatar.session.state import SessionState

MAX_CYCLES = 2
MAX_TOKENS = 400
TEMPERATURE = 0.2


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
        self._client = client or AsyncOpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
        self._tools = tools or InternalTools(recs, lane, catalog, timeout_s=settings.tool_timeout_s)
        self._turn_task: asyncio.Task | None = None
        self._turn_id: str | None = None
        self._interrupted = False

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
        if self._turn_id is not None:
            dropped = self._bus.cancel_turn(self._turn_id)
            logger.bind(session_id=self._session.session_id, turn_id=self._turn_id).info(
                "turn interrupted", dropped_commands=dropped)

    # --- the turn -----------------------------------------------------------

    async def _run_turn(self, context: LLMContext) -> None:
        turn_id = self._session.new_turn()
        self._turn_id = turn_id
        self._interrupted = False
        self._turn_task = asyncio.create_task(self._turn(context, turn_id))
        try:
            await self._turn_task
        except asyncio.CancelledError:
            if not self._interrupted:
                raise
        finally:
            self._turn_task = None

    async def _turn(self, context: LLMContext, turn_id: str) -> None:
        user_id = self._session.user_id or self._session.session_id
        log = logger.bind(session_id=self._session.session_id, user_id=user_id, turn_id=turn_id)
        t0 = time.perf_counter()
        marks: dict[str, Any] = {"cycles": 0, "n_actions": 0, "intent": None}

        await self.start_processing_metrics()
        messages = list(context.get_messages())
        user_text = _last_user_text(messages)
        memory = await self._lane.recall(user_id, user_text)
        history_text = (await self._history.render_for_prompt(user_id, self._catalog)
                        if self._history is not None else "Recently watched: (none yet)")
        messages = self.build_messages(messages, memory, history_text)
        marks["recall_ms"] = round((time.perf_counter() - t0) * 1000)

        await self.push_frame(LLMFullResponseStartFrame())
        memory_text = None if memory.empty else memory.render_for_prompt()
        for cycle in range(1, MAX_CYCLES + 1):
            marks["cycles"] = cycle
            raw, results = await self._cycle(messages, turn_id, user_id, memory_text, cycle, marks, t0)
            if not self.needs_second_cycle(results) or cycle == MAX_CYCLES:
                break
            messages = messages + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": "[tool results]\n" + json.dumps(
                    {verb: result for verb, result in results}, ensure_ascii=False)
                    + "\nNow answer the user using these results. Do not call internal tools again."},
            ]
        await self.push_frame(LLMFullResponseEndFrame())
        await self.stop_processing_metrics()
        marks["total_ms"] = round((time.perf_counter() - t0) * 1000)
        log.info("turn", **marks)

    async def _cycle(self, messages: list[dict], turn_id: str, user_id: str, memory_text: str | None,
                     cycle: int, marks: dict, t0: float) -> tuple[str, list[tuple[str, dict]]]:
        streamer = EnvelopeStreamer()
        raw: list[str] = []
        awaited: list[tuple[str, asyncio.Task]] = []
        fire: list[asyncio.Task] = []
        first_say = True

        await self.start_ttfb_metrics()
        stream = await self._client.chat.completions.create(
            model=self._cfg.llm_model, messages=messages, stream=True,
            temperature=TEMPERATURE, max_tokens=MAX_TOKENS,
            response_format={"type": "json_schema", "json_schema": turn_plan_schema()},
            extra_body=self._cfg.llm_extra_body or None,
        )
        try:
            async for chunk in stream:
                choice = chunk.choices[0] if chunk.choices else None
                delta = choice.delta.content if choice and choice.delta else None
                if not delta:
                    continue
                raw.append(delta)
                for event in streamer.feed(delta):
                    match event:
                        case IntentReady(intent=intent):
                            marks["intent"] = intent
                        case SayDelta(text=text):
                            if first_say:
                                first_say = False
                                await self.stop_ttfb_metrics()
                                marks.setdefault("ttft_ms", round((time.perf_counter() - t0) * 1000))
                            await self.push_frame(LLMTextFrame(text))
                        case ActionReady(action=action):
                            verb = str(action.get("verb", ""))
                            args = {k: v for k, v in action.items() if k != "verb" and v is not None}
                            marks["n_actions"] += 1
                            if verb in INTERNAL_AWAIT and cycle >= MAX_CYCLES:
                                continue  # no open-ended loops on a voice interface
                            task = asyncio.create_task(
                                self.dispatch_action(verb, args, turn_id, user_id, memory_text))
                            if verb in AWAITED_VERBS:
                                awaited.append((verb, task))
                            else:
                                fire.append(task)
                            marks.setdefault("first_action_ms", round((time.perf_counter() - t0) * 1000))
                        case Done():
                            break
                if streamer.finished:
                    break
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                await close()
        if first_say:
            await self.stop_ttfb_metrics()

        results: list[tuple[str, dict]] = []
        for verb, task in awaited:
            try:
                results.append((verb, await task))
            except Exception as err:  # noqa: BLE001
                results.append((verb, {"status": "error", "reason": type(err).__name__}))
        for task in fire:
            with contextlib.suppress(Exception):  # failures are logged in dispatch_action
                await task
        return "".join(raw), results

    # --- pieces the tests call directly -------------------------------------

    def build_messages(self, messages: list[dict], memory: MemoryBlock, history_summary: str) -> list[dict]:
        rest = [m for m in messages if m.get("role") != "system"]
        existing = next((m for m in messages if m.get("role") == "system"), None)
        if existing is not None and isinstance(existing.get("content"), str) and "# Screen" in existing["content"]:
            static = existing["content"]
            volatile = "\n\n".join([f"# Memory\n{memory.render_for_prompt()}", f"# Recent activity\n{history_summary}"])
        else:
            static = build_system_prompt()
            volatile = volatile_sections(self._session.render_for_prompt(), memory.render_for_prompt(), history_summary)
        return [{"role": "system", "content": static + "\n\n" + volatile}, *rest]

    async def dispatch_action(self, verb: str, args: dict, turn_id: str, user_id: str = "",
                              memory_text: str | None = None) -> dict:
        log = logger.bind(session_id=self._session.session_id, user_id=user_id, turn_id=turn_id)
        if verb in INTERNAL_AWAIT:
            result = await self._tools.run(verb, args, user_id, memory_text)
            log.info("internal tool", verb=verb, status=result.get("status", "ok"),
                     n=len(result.get("titles", [])))
            return result
        try:
            result = await self._bus.dispatch(verb, args, turn_id=turn_id)
        except ValueError as err:
            log.warning("rejected action", verb=verb, reason=str(err))
            return {"status": "invalid", "reason": str(err)}
        log.info("tv command", verb=verb, status=result.get("status"))
        return result

    @staticmethod
    def needs_second_cycle(results: list[tuple[str, dict]]) -> bool:
        """Any internal tool call earns a second cycle — including a failed one,
        so the agent speaks the fallback instead of stopping at the filler."""
        return any(verb in INTERNAL_AWAIT for verb, _ in results)
