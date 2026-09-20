"""The SGR loop, independent of Pipecat: plan → act → observe, per cycle.

`TurnRunner.run` streams one envelope per cycle, speaks `say` sentence by
sentence as it arrives and dispatches each `actions[]` element the moment it
closes. The *model* decides whether the turn goes on: a cycle that produced an
observation (an awaited tool's reply, including a TV search result) buys the
next cycle with that observation fed back; a cycle that produced none was the
answer. The loop itself only iterates.

The cap is the schema's job, not the loop's: the last allowed cycle is decoded
against `turn_plan_schema(final=True)`, which has no observation-returning
tools, so it cannot ask for more. Every cycle after the first must produce its
first `say` byte within `cycle_first_byte_s` or the previous observations are
spoken from a template — the turn never outlives its budget. Speaking,
dispatching and TTFB metrics go through the `TurnHost` protocol, which the
Pipecat service implements.
"""
import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from opentelemetry.trace import Span
from pipecat.utils.text.simple_text_aggregator import SimpleTextAggregator
from pydantic import BaseModel, ValidationError

from tv_avatar.agent.envelope import REGISTRY, parse_action, turn_plan_schema
from tv_avatar.agent.fallback import render_fallback
from tv_avatar.agent.prompt import tool_results_message
from tv_avatar.agent.stream_parse import (
    ActionReady,
    Done,
    EnvelopeStreamer,
    Event,
    IntentReady,
    SayDelta,
    SayDone,
)
from tv_avatar.agent.turn import (
    CycleOutcome,
    ToolResult,
    TurnContext,
    TurnMetrics,
    TurnTrace,
)
from tv_avatar.config import Settings
from tv_avatar.tracing import (
    ATTR_CYCLE_BUDGET_MS,
    ATTR_CYCLE_MAX,
    ATTR_CYCLE_N_ACTIONS,
    ATTR_CYCLE_N_REJECTED,
    ATTR_CYCLE_TTFT_MS,
    ATTR_FALLBACK_N_ACTIONS,
    ATTR_GENAI_INPUT_TOKENS,
    ATTR_GENAI_MAX_TOKENS,
    ATTR_GENAI_MODEL,
    ATTR_GENAI_OUTPUT_TOKENS,
    ATTR_GENAI_TEMPERATURE,
    ATTR_OBS_INPUT,
    ATTR_OBS_LEVEL,
    ATTR_OBS_OUTPUT,
    EVENT_ACTION_REJECTED,
    LEVEL_WARNING,
    META_CYCLE,
    META_OVER_BUDGET,
    OBS_TYPE_GENERATION,
    OBS_TYPE_SPAN,
    observation,
)

MAX_TOKENS = 220           # say is 1–2 spoken sentences; actions are small
TEMPERATURE = 0.2


class CycleOverBudget(Exception):
    """A follow-up cycle produced no `say` byte within `cycle_first_byte_s`."""


class TurnHost(Protocol):
    """What the loop needs from its Pipecat host."""
    async def speak(self, text: str) -> None: ...
    async def dispatch_action(self, action: BaseModel, ctx: TurnContext) -> dict: ...
    async def start_ttfb_metrics(self) -> None: ...
    async def stop_ttfb_metrics(self) -> None: ...


@dataclass
class _Cycle:
    """Mutable state of the cycle being streamed."""
    ctx: TurnContext
    n: int
    final: bool
    span: Span
    t_req: float = field(default_factory=time.perf_counter)
    sentences: SimpleTextAggregator = field(default_factory=SimpleTextAggregator)  # the TTS splitter, but we own the flush
    said: list[str] = field(default_factory=list)
    awaited: list[tuple[str, asyncio.Task]] = field(default_factory=list)
    fire: list[asyncio.Task] = field(default_factory=list)
    n_rejected: int = 0
    first_say_pending: bool = True

    def ms(self) -> int:
        return round((time.perf_counter() - self.t_req) * 1000)


class TurnRunner:
    def __init__(self, client: Any, cfg: Settings, host: TurnHost) -> None:
        self._client = client
        self._cfg = cfg
        self._host = host

    async def run(self, messages: list[dict], ctx: TurnContext, metrics: TurnMetrics, trace: TurnTrace) -> None:
        max_cycles = self._cfg.agent_max_cycles
        previous: CycleOutcome | None = None
        for cycle in range(1, max_cycles + 1):
            budget_s = None if cycle == 1 else self._cfg.cycle_first_byte_s
            ctx.log.debug("cycle start", step="cycle", cycle=cycle, n_messages=len(messages),
                          budget_ms=None if budget_s is None else round(budget_s * 1000))
            try:
                outcome = await self._cycle(messages, ctx, cycle, metrics, trace,
                                            final=cycle == max_cycles, budget_s=budget_s)
            except CycleOverBudget:
                metrics.fallback = True
                assert previous is not None  # cycle 1 has no budget
                await self._speak_fallback(previous.observations, ctx, metrics, trace)
                return
            trace.add_results(outcome.observations)
            ctx.log.debug("cycle end", step="cycle", cycle=cycle, raw=outcome.raw, done=outcome.done,
                          observed=[r.verb for r in outcome.observations])
            if outcome.done:
                return
            feedback = outcome.feedback()
            ctx.log.debug("observations fed back", step="feedback", results=feedback)
            messages = [*messages, {"role": "assistant", "content": outcome.raw},
                        {"role": "user", "content": tool_results_message(feedback)}]
            previous = outcome

    async def _speak_fallback(self, results: tuple[ToolResult, ...], ctx: TurnContext,
                              metrics: TurnMetrics, trace: TurnTrace) -> None:
        """Speak the templated answer; recorded on `trace.fallback_said`, not `trace.said`."""
        text, actions = render_fallback(results)
        ctx.log.debug("fallback", step="say", text=text, actions=actions)
        trace.fallback_said = text
        with observation("agent.fallback", type=OBS_TYPE_SPAN, **{
            META_CYCLE: metrics.cycles, ATTR_OBS_LEVEL: LEVEL_WARNING,
            ATTR_FALLBACK_N_ACTIONS: len(actions), ATTR_OBS_OUTPUT: text,
        }):
            await self._host.speak(text)
            for verb, args in actions:
                trace.add_action(verb, args, after_results=True)
                await self._host.dispatch_action(parse_action({"verb": verb, **args}), ctx)

    async def _cycle(self, messages: list[dict], ctx: TurnContext, cycle: int, metrics: TurnMetrics,
                     trace: TurnTrace, *, final: bool, budget_s: float | None) -> CycleOutcome:
        # One LLM call = one `generation` (D19). The raw envelope is recorded in
        # the `finally` so a budget-cancelled cycle still shows what it streamed.
        raw: list[str] = []
        with observation("agent.cycle", type=OBS_TYPE_GENERATION, **{
            META_CYCLE: cycle, ATTR_CYCLE_MAX: self._cfg.agent_max_cycles,
            ATTR_GENAI_MODEL: self._cfg.llm_model, ATTR_GENAI_TEMPERATURE: TEMPERATURE,
            ATTR_GENAI_MAX_TOKENS: MAX_TOKENS, ATTR_OBS_INPUT: json.dumps(messages, ensure_ascii=False),
        }) as span:
            state = _Cycle(ctx, cycle, final, span)
            try:
                return await self._run_cycle(messages, state, metrics, trace, raw, budget_s)
            except TimeoutError:
                budget_ms = round((budget_s or 0) * 1000)
                span.set_attributes({META_OVER_BUDGET: True, ATTR_CYCLE_BUDGET_MS: budget_ms})
                ctx.log.warning("cycle over budget; speaking templated answer", step="cycle", cycle=cycle,
                                budget_ms=budget_ms)
                raise CycleOverBudget(cycle) from None
            finally:
                tasks = [task for _, task in state.awaited] + state.fire
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                span.set_attribute(ATTR_OBS_OUTPUT, "".join(raw))

    async def _run_cycle(self, messages: list[dict], c: _Cycle, metrics: TurnMetrics, trace: TurnTrace,
                         raw: list[str], budget_s: float | None) -> CycleOutcome:
        host = self._host
        metrics.cycles = max(metrics.cycles, c.n)  # attempted, even if cancelled over budget
        trace.begin_cycle()
        await host.start_ttfb_metrics()
        # The budget covers only the wait for the first spoken byte: once the
        # viewer hears something the cycle may take as long as it needs.
        async with asyncio.timeout(budget_s) as deadline, \
                contextlib.aclosing(self._stream(messages, c, raw)) as events:
            async for event in events:
                match event:
                    case IntentReady(intent=intent):
                        metrics.mark_once("intent", intent)
                        c.ctx.log.debug("intent", step="intent", cycle=c.n, intent=intent, ms=c.ms())
                    case SayDelta(text=text):
                        if c.first_say_pending:
                            deadline.reschedule(None)
                            await self._first_say(c, metrics)
                        await self._say(text, c, trace)
                    case SayDone():
                        # The say string closed: speak the tail now rather
                        # than after the actions array (or the next cycle).
                        await self._flush(c)
                    case ActionReady(action=raw_action):
                        metrics.n_actions += 1
                        self._start_action(raw_action, c, metrics, trace)
                    case Done():
                        break
        await self._flush(c)  # say never closed: truncated or malformed envelope
        if c.first_say_pending:
            await host.stop_ttfb_metrics()
        c.ctx.log.debug("stream done", step="say", cycle=c.n, say="".join(c.said), chunks=len(raw),
                        awaited=len(c.awaited), fire_and_forget=len(c.fire), ms=c.ms())
        c.span.set_attributes({ATTR_CYCLE_N_ACTIONS: len(c.awaited) + len(c.fire),
                               ATTR_CYCLE_N_REJECTED: c.n_rejected})
        return CycleOutcome("".join(raw), await self._observe(c))

    async def _stream(self, messages: list[dict], c: _Cycle, raw: list[str]) -> AsyncIterator[Event]:
        """The LLM call as envelope events. Knows the wire, not the product."""
        streamer = EnvelopeStreamer()
        stream = None
        try:
            stream = await self._client.chat.completions.create(
                model=self._cfg.llm_model, messages=messages, stream=True,
                temperature=TEMPERATURE, max_tokens=MAX_TOKENS,
                response_format={"type": "json_schema", "json_schema": turn_plan_schema(final=c.final)},
                extra_body=self._cfg.llm_extra_body or None,
            )
            async for chunk in stream:
                # Usage rides on a trailing chunk when the endpoint sends one
                # (D21: not requested via stream_options until verified live).
                if (usage := getattr(chunk, "usage", None)) is not None:
                    c.span.set_attributes({ATTR_GENAI_INPUT_TOKENS: usage.prompt_tokens,
                                           ATTR_GENAI_OUTPUT_TOKENS: usage.completion_tokens})
                choice = chunk.choices[0] if chunk.choices else None
                delta = choice.delta.content if choice and choice.delta else None
                if not delta:
                    continue
                raw.append(delta)
                for event in streamer.feed(delta):
                    yield event
                if streamer.finished:
                    return
        finally:
            close = getattr(stream, "close", None)  # also on cancellation during create()
            if close is not None:
                await close()

    async def _first_say(self, c: _Cycle, metrics: TurnMetrics) -> None:
        c.first_say_pending = False
        await self._host.stop_ttfb_metrics()
        metrics.mark_once("ttft_ms", c.ctx.elapsed_ms())
        c.span.set_attribute(ATTR_CYCLE_TTFT_MS, c.ms())
        c.ctx.log.debug("first say byte", step="say", cycle=c.n, ms=c.ms())

    async def _say(self, text: str, c: _Cycle, trace: TurnTrace) -> None:
        c.said.append(text)
        trace.said.append(text)
        async for sentence in c.sentences.aggregate(text):
            c.ctx.log.debug("sentence -> TTS", step="say", cycle=c.n, text=sentence.text, ms=c.ms())
            await self._host.speak(sentence.text)

    async def _flush(self, c: _Cycle) -> None:
        if (tail := await c.sentences.flush()) is not None:
            c.ctx.log.debug("sentence -> TTS", step="say", cycle=c.n, text=tail.text, ms=c.ms())
            await self._host.speak(tail.text)

    def _start_action(self, raw_action: dict, c: _Cycle, metrics: TurnMetrics, trace: TurnTrace) -> None:
        """Validate against the union this cycle was decoded with and dispatch.
        On the final cycle an observation tool is unrepresentable for the
        decoder; a canned envelope that still carries one is rejected here."""
        try:
            action = parse_action(raw_action, final=c.final)
        except ValidationError as err:
            reason = str(err).splitlines()[0]
            c.ctx.log.warning("rejected action", verb=raw_action.get("verb"), reason=reason)
            c.n_rejected += 1
            c.span.add_event(EVENT_ACTION_REJECTED, {"verb": str(raw_action.get("verb")), "reason": reason})
            return
        verb, spec = str(action.verb), REGISTRY[str(action.verb)]
        args = action.model_dump(exclude={"verb"}, exclude_none=True)
        c.ctx.log.debug("action ready", step="action", cycle=c.n, verb=verb, args=args,
                        awaited=spec.awaits_result, ms=c.ms())
        trace.add_action(verb, args, after_results=c.n > 1)
        task = asyncio.create_task(self._host.dispatch_action(action, c.ctx))
        if spec.awaits_result:
            c.awaited.append((verb, task))
        else:
            c.fire.append(task)
        metrics.mark_once("first_action_ms", c.ctx.elapsed_ms())

    async def _observe(self, c: _Cycle) -> tuple[ToolResult, ...]:
        """Collect the awaited replies; keep the ones the model must see."""
        results: list[ToolResult] = []
        for verb, task in c.awaited:
            try:
                results.append(ToolResult(verb, await task))
            except Exception as err:  # noqa: BLE001
                c.ctx.log.opt(exception=err).debug("awaited action raised", step="action", verb=verb)
                results.append(ToolResult(verb, {"status": "error", "reason": type(err).__name__}))
        for task in c.fire:
            with contextlib.suppress(Exception):  # failures are logged in dispatch_action
                await task
        return tuple(r for r in results if r.is_observation)
