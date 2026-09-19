"""The SGR cycle loop, independent of Pipecat.

`TurnRunner.run` streams one envelope per cycle, speaks `say` sentence by
sentence as it arrives, dispatches each `actions[]` element the moment it
closes, and feeds cycle-earning tool results back for the next cycle — up to
`agent_max_cycles`, every follow-up cycle budgeted by `cycle_first_byte_s`
with a templated fallback. Speaking, dispatching and TTFB metrics go through
the `TurnHost` protocol, which the Pipecat service implements.
"""
import asyncio
import contextlib
import time
from typing import Any, Protocol

from pipecat.utils.text.simple_text_aggregator import SimpleTextAggregator
from pydantic import BaseModel, ValidationError

from tv_avatar.agent.envelope import REGISTRY, parse_action, turn_plan_schema
from tv_avatar.agent.fallback import render_fallback
from tv_avatar.agent.prompt import tool_results_message
from tv_avatar.agent.stream_parse import (
    ActionReady,
    Done,
    EnvelopeStreamer,
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

MAX_TOKENS = 220           # say is 1–2 spoken sentences; actions are small
TEMPERATURE = 0.2


class TurnHost(Protocol):
    """What the loop needs from its Pipecat host."""
    async def speak(self, text: str) -> None: ...
    async def dispatch_action(self, action: BaseModel, ctx: TurnContext) -> dict: ...
    async def start_ttfb_metrics(self) -> None: ...
    async def stop_ttfb_metrics(self) -> None: ...


class TurnRunner:
    def __init__(self, client: Any, cfg: Settings, host: TurnHost) -> None:
        self._client = client
        self._cfg = cfg
        self._host = host

    async def run(self, messages: list[dict], ctx: TurnContext, metrics: TurnMetrics, trace: TurnTrace) -> str:
        """Run the cycles for one turn. Returns the templated fallback text if one
        was spoken (it is not part of `trace.said`), else ""."""
        log = ctx.log
        log.debug("cycle start", step="cycle", cycle=1, n_messages=len(messages))
        outcome = await self._cycle(messages, ctx, 1, metrics, trace)
        log.debug("cycle envelope", step="cycle", cycle=1, raw=outcome.raw)
        trace.add_results(outcome.results)
        fallback_text = ""
        # A cycle-earning tool result buys one more LLM cycle, up to the cap.
        # Every follow-up cycle must start speaking within its budget or the
        # results are spoken from a template — the loop never outlives it.
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
            budgeted = await self._cycle_with_budget(messages, ctx, cycle, metrics, trace)
            if budgeted is None:
                metrics.fallback = True
                fallback_text = await self._speak_fallback(outcome.results, ctx, trace)
                break
            outcome = budgeted
            trace.add_results(outcome.results)
        log.debug("cycle end", step="cycle", cycle=metrics.cycles, next_cycle=False,
                  awaited=[r.verb for r in outcome.results])
        return fallback_text

    async def _cycle_with_budget(self, messages: list[dict], ctx: TurnContext, cycle: int,
                                 metrics: TurnMetrics, trace: TurnTrace) -> CycleOutcome | None:
        """Run a follow-up cycle; None if it produced no speech within `cycle_first_byte_s`."""
        budget_s = self._cfg.cycle_first_byte_s
        first_say = asyncio.Event()
        ctx.log.debug("cycle start", step="cycle", cycle=cycle, n_messages=len(messages),
                      budget_ms=round(budget_s * 1000))
        task = asyncio.create_task(self._cycle(messages, ctx, cycle, metrics, trace, first_say=first_say))
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

    async def _speak_fallback(self, results: tuple[ToolResult, ...], ctx: TurnContext, trace: TurnTrace) -> str:
        """Speak the templated answer; returns its text for the offered-titles log."""
        text, actions = render_fallback(results)
        ctx.log.debug("fallback", step="say", text=text, actions=actions)
        # Deliberately not added to `trace.said`: a template built from substitute
        # results is not the agent's reply, and the memory profile must not learn
        # the viewer "wanted" whatever the popular channel happened to return.
        await self._host.speak(text)
        for verb, args in actions:
            trace.add_action(verb, args, after_results=True)
            await self._host.dispatch_action(parse_action({"verb": verb, **args}), ctx)
        return text

    async def _cycle(self, messages: list[dict], ctx: TurnContext, cycle: int, metrics: TurnMetrics,
                     trace: TurnTrace, first_say: asyncio.Event | None = None) -> CycleOutcome:
        log, host = ctx.log, self._host
        metrics.cycles = max(metrics.cycles, cycle)  # attempted, even if cancelled over budget
        if cycle > 1 and trace.said:
            trace.said.append(" ")  # the memory transcript reads "Let me look. I found…", not "look.I found"
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

        await host.start_ttfb_metrics()
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
                            metrics.mark_once("intent", intent)
                            log.debug("intent", step="intent", cycle=cycle, intent=intent, ms=ms())
                        case SayDelta(text=text):
                            said.append(text)
                            trace.said.append(text)
                            if first_say_pending:
                                first_say_pending = False
                                if first_say is not None:
                                    first_say.set()
                                await host.stop_ttfb_metrics()
                                metrics.mark_once("ttft_ms", ctx.elapsed_ms())
                                log.debug("first say byte", step="say", cycle=cycle, ms=ms())
                            async for sentence in sentences.aggregate(text):
                                log.debug("sentence -> TTS", step="say", cycle=cycle, text=sentence.text, ms=ms())
                                await host.speak(sentence.text)
                        case SayDone():
                            # The say string closed: speak the tail now rather
                            # than after the actions array (or the next cycle).
                            if (tail := await sentences.flush()) is not None:
                                log.debug("sentence -> TTS", step="say", cycle=cycle, text=tail.text, ms=ms())
                                await host.speak(tail.text)
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
                            args = action.model_dump(exclude={"verb"}, exclude_none=True)
                            log.debug("action ready", step="action", cycle=cycle, verb=verb, args=args,
                                      awaited=spec.awaits_result, ms=ms())
                            trace.add_action(verb, args, after_results=cycle > 1)
                            task = asyncio.create_task(host.dispatch_action(action, ctx))
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
            await host.speak(tail.text)
        if first_say_pending:
            await host.stop_ttfb_metrics()
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
