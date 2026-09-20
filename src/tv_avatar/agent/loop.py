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
import hashlib
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from opentelemetry.trace import Span, StatusCode
from pipecat.utils.text.simple_text_aggregator import SimpleTextAggregator
from pydantic import BaseModel, ValidationError

from tv_avatar import tracing as tel
from tv_avatar.agent.envelope import (
    REGISTRY,
    Request,
    action_violation,
    parse_action,
    turn_plan_schema,
)
from tv_avatar.agent.fallback import render_fallback
from tv_avatar.agent.prompt import tool_results_message
from tv_avatar.agent.stream_parse import (
    ActionReady,
    Done,
    EnvelopeStreamer,
    Event,
    IntentReady,
    RequestReady,
    SayDelta,
    SayDone,
)
from tv_avatar.agent.turn import (
    CycleOutcome,
    CycleTelemetry,
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
    request: Request | None = None
    telemetry: CycleTelemetry = field(default_factory=CycleTelemetry)

    def ms(self) -> int:
        return round((time.perf_counter() - self.t_req) * 1000)


class TurnRunner:
    def __init__(self, client: Any, cfg: Settings, host: TurnHost) -> None:
        self._client = client
        self._cfg = cfg
        self._host = host
        self._schemas = {final: turn_plan_schema(final=final) for final in (False, True)}
        self._schema_hashes = {final: hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()
                               for final, schema in self._schemas.items()}

    async def run(self, messages: list[dict], ctx: TurnContext, metrics: TurnMetrics, trace: TurnTrace) -> None:
        max_cycles = self._cfg.agent_max_cycles
        previous: CycleOutcome | None = None
        for cycle in range(1, max_cycles + 1):
            budget_s = None if cycle == 1 else self._cfg.cycle_first_byte_s or None
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
            with observation("agent.feedback", type=tel.OBS_TYPE_EVENT, **{
                META_CYCLE: cycle, tel.META_TURN_ID: ctx.turn_id, ATTR_OBS_OUTPUT: feedback,
            }):
                pass
            messages = [*messages, {"role": "assistant", "content": outcome.raw},
                        {"role": "user", "content": tool_results_message(
                            feedback, original_request=trace.user_text)}]
            previous = outcome

    async def _speak_fallback(self, results: tuple[ToolResult, ...], ctx: TurnContext,
                              metrics: TurnMetrics, trace: TurnTrace) -> None:
        """Speak the templated answer; recorded on `trace.fallback_said`, not `trace.said`."""
        text, actions = render_fallback(results)
        ctx.log.debug("fallback", step="say", text=text, actions=actions)
        trace.fallback_said = text
        with tel.attribute_scope({tel.META_TURN_ID: ctx.turn_id, META_CYCLE: metrics.cycles}), \
                observation("agent.fallback", type=OBS_TYPE_SPAN, **{
            META_CYCLE: metrics.cycles, ATTR_OBS_LEVEL: LEVEL_WARNING,
            ATTR_FALLBACK_N_ACTIONS: len(actions), ATTR_OBS_OUTPUT: text,
        }):
            await self._submit(text, ctx, trace, cycle=metrics.cycles, source="fallback", index=0)
            for index, (verb, args) in enumerate(actions):
                trace.add_action(verb, args, after_results=True)
                with tel.attribute_scope({tel.META_ACTION_INDEX: index, tel.META_SOURCE: "fallback"}):
                    await self._host.dispatch_action(parse_action({"verb": verb, **args}), ctx)

    async def _cycle(self, messages: list[dict], ctx: TurnContext, cycle: int, metrics: TurnMetrics,
                     trace: TurnTrace, *, final: bool, budget_s: float | None) -> CycleOutcome:
        # One LLM call = one `generation` (D19). The raw envelope is recorded in
        # the `finally` so a budget-cancelled cycle still shows what it streamed.
        raw: list[str] = []
        schema = self._schemas[final]
        with tel.attribute_scope({tel.META_TURN_ID: ctx.turn_id, META_CYCLE: cycle}), \
                observation("agent.cycle", type=OBS_TYPE_GENERATION, **{
            META_CYCLE: cycle, ATTR_CYCLE_MAX: self._cfg.agent_max_cycles,
            tel.META_TURN_ID: ctx.turn_id, tel.META_FINAL_CYCLE: final,
            tel.META_SCHEMA_NAME: schema["name"],
            tel.META_SCHEMA_HASH: self._schema_hashes[final],
            tel.META_PROVIDER: urlsplit(self._cfg.nebius_base_url).hostname or "unknown",
            ATTR_GENAI_MODEL: self._cfg.llm_model, ATTR_GENAI_TEMPERATURE: TEMPERATURE,
            ATTR_GENAI_MAX_TOKENS: MAX_TOKENS, ATTR_OBS_INPUT: json.dumps(messages, ensure_ascii=False),
        }) as span:
            state = _Cycle(ctx, cycle, final, span)
            try:
                return await self._run_cycle(messages, state, metrics, trace, raw, budget_s)
            except asyncio.CancelledError:
                state.telemetry.stop_reason = "interrupted"
                raise
            except TimeoutError:
                if state.telemetry.stop_reason == "provider_error" or budget_s is None:
                    raise
                state.telemetry.stop_reason = "budget_exceeded"
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
                output = "".join(raw)
                state.telemetry.total_ms = state.ms()
                plan = state.telemetry.plan(output, final=final)
                span.set_attributes({ATTR_OBS_OUTPUT: output,
                                     ATTR_CYCLE_N_ACTIONS: len(state.telemetry.accepted),
                                     ATTR_CYCLE_N_REJECTED: state.n_rejected,
                                     tel.ATTR_ENVELOPE_COMPLETE: state.telemetry.envelope_complete,
                                     **state.telemetry.as_span_attributes()})
                with observation("agent.plan", type=tel.OBS_TYPE_EVENT, **{
                    META_CYCLE: cycle, tel.META_TURN_ID: ctx.turn_id,
                    tel.META_VALIDATION: state.telemetry.validation,
                    ATTR_OBS_OUTPUT: json.dumps(plan, ensure_ascii=False),
                }):
                    pass
                if state.telemetry.validation != "valid":
                    span.set_attribute(ATTR_OBS_LEVEL, LEVEL_WARNING)
                    ctx.log.warning("agent.plan.invalid", event="agent.plan.invalid", cycle=cycle,
                                    validation=state.telemetry.validation)
                ctx.log.info("agent.cycle.completed", event="agent.cycle.completed", cycle=cycle,
                             **state.telemetry.as_log_fields())

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
                    case RequestReady(request=raw_request):
                        self._decode_request(raw_request, c, metrics)
                    case SayDelta(text=text):
                        if c.first_say_pending:
                            deadline.reschedule(None)
                            await self._first_say(c, metrics)
                        await self._say(text, c, trace)
                    case SayDone():
                        # The say string closed: speak the tail now rather
                        # than after the actions array (or the next cycle).
                        await self._flush(c, trace)
                    case ActionReady(action=raw_action):
                        metrics.n_actions += 1
                        self._start_action(raw_action, c, metrics, trace)
                    case Done():
                        break
        await self._flush(c, trace)  # say never closed: truncated or malformed envelope
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
                response_format={"type": "json_schema", "json_schema": self._schemas[c.final]},
                extra_body=self._cfg.llm_extra_body or None,
            )
            request_id = getattr(stream, "_request_id", None)
            if isinstance(request_id, str):
                c.span.set_attribute(tel.ATTR_REQUEST_ID, request_id)
            async for chunk in stream:
                for key, name in ((tel.ATTR_RESPONSE_MODEL, "model"), (tel.ATTR_RESPONSE_ID, "id")):
                    value = getattr(chunk, name, None)
                    if isinstance(value, str):
                        c.span.set_attribute(key, value)
                # Usage rides on a trailing chunk when the endpoint sends one
                # (D21: not requested via stream_options until verified live).
                if (usage := getattr(chunk, "usage", None)) is not None:
                    c.telemetry.usage_available = True
                    c.span.set_attributes({ATTR_GENAI_INPUT_TOKENS: usage.prompt_tokens,
                                           ATTR_GENAI_OUTPUT_TOKENS: usage.completion_tokens})
                choice = chunk.choices[0] if chunk.choices else None
                if (finish := getattr(choice, "finish_reason", None)) is not None:
                    c.telemetry.finish_reason = finish
                delta = choice.delta.content if choice and choice.delta else None
                if not delta:
                    continue
                if c.telemetry.first_content_ms is None:
                    c.telemetry.first_content_ms = c.ms()
                raw.append(delta)
                events = streamer.feed(delta)
                if streamer.finished:
                    c.telemetry.envelope_complete = True
                    c.telemetry.stop_reason = "envelope_complete"
                for event in events:
                    yield event
                if streamer.finished:
                    return
        except Exception as err:
            c.telemetry.stop_reason = "provider_error"
            c.span.set_attribute(tel.ATTR_ERROR_TYPE, type(err).__name__)
            c.span.set_status(StatusCode.ERROR, type(err).__name__)
            raise
        finally:
            c.telemetry.generation_ms = c.ms()
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
            await self._sentence(sentence.text, c, trace)

    async def _flush(self, c: _Cycle, trace: TurnTrace) -> None:
        if (tail := await c.sentences.flush()) is not None:
            c.ctx.log.debug("sentence -> TTS", step="say", cycle=c.n, text=tail.text, ms=c.ms())
            await self._sentence(tail.text, c, trace)

    async def _sentence(self, text: str, c: _Cycle, trace: TurnTrace) -> None:
        await self._submit(text, c.ctx, trace, cycle=c.n, source="model",
                           index=c.telemetry.sentence_index)
        c.telemetry.sentence_index += 1
        if c.telemetry.first_sentence_ms is None:
            c.telemetry.first_sentence_ms = c.ms()

    async def _submit(self, text: str, ctx: TurnContext, trace: TurnTrace,
                      *, cycle: int, source: str, index: int) -> None:
        with observation("agent.speech", type=tel.OBS_TYPE_EVENT, **{
            META_CYCLE: cycle, tel.META_TURN_ID: ctx.turn_id, tel.META_SOURCE: source,
            tel.ATTR_SENTENCE_INDEX: index, tel.META_STATUS: "submitting",
        }) as span:
            try:
                await self._host.speak(text)
            except asyncio.CancelledError:
                span.set_attribute(tel.META_STATUS, "cancelled")
                raise
            except Exception:
                span.set_attribute(tel.META_STATUS, "failed")
                raise
            trace.submitted.append(text.strip())
            span.set_attributes({tel.META_STATUS: "submitted", ATTR_OBS_OUTPUT: text.strip()})
            ctx.log.debug("agent.speech.submitted", event="agent.speech.submitted", cycle=cycle,
                          source=source, sentence_index=index, text=text.strip())

    def _decode_request(self, raw_request: dict, c: _Cycle, metrics: TurnMetrics) -> None:
        """The cascade's second step. Constrained decoding guarantees the shape,
        so a failure here is a canned or truncated envelope: the actions then run
        unchecked and the plan is reported invalid at the end of the cycle."""
        try:
            c.request = Request.model_validate(raw_request)
        except ValidationError as err:
            c.ctx.log.warning("undecodable request", step="request", cycle=c.n,
                              reason=str(err).splitlines()[0])
            return
        metrics.mark_once("operation", c.request.operation)
        c.span.set_attribute(tel.META_OPERATION, c.request.operation)
        c.ctx.log.debug("request", step="request", cycle=c.n, ms=c.ms(),
                        **c.request.model_dump(exclude_none=True))

    def _start_action(self, raw_action: dict, c: _Cycle, metrics: TurnMetrics, trace: TurnTrace) -> None:
        """Validate against the union this cycle was decoded with, then against
        the decoded request, and dispatch. On the final cycle an observation
        tool is unrepresentable for the decoder; a canned envelope that still
        carries one is rejected here. An action the request does not license
        (`play` for a lookup, `show_products` for a watch) is rejected the same
        way: the viewer hears `say`, and nothing they did not ask for happens."""
        index = len(c.telemetry.accepted) + len(c.telemetry.rejected)
        try:
            action = parse_action(raw_action, final=c.final)
        except ValidationError as err:
            errors = [{"path": list(e["loc"]), "code": e["type"]}
                      for e in err.errors(include_input=False, include_context=False)]
            self._reject(raw_action, c, index, str(err).splitlines()[0], errors)
            return
        if c.request is not None and (reason := action_violation(c.request, action)):
            self._reject(raw_action, c, index, reason, [{"path": ["actions", index], "code": "off_request"}])
            return
        verb, spec = str(action.verb), REGISTRY[str(action.verb)]
        args = action.model_dump(exclude={"verb"}, exclude_none=True)
        c.ctx.log.debug("action ready", step="action", cycle=c.n, verb=verb, args=args,
                        awaited=spec.awaits_result, ms=c.ms())
        trace.add_action(verb, args, after_results=c.n > 1)
        c.telemetry.accepted.append({"action_index": index, "action": action.model_dump()})
        with tel.attribute_scope({tel.META_ACTION_INDEX: index}):
            task = asyncio.create_task(self._host.dispatch_action(action, c.ctx))
        if spec.awaits_result:
            c.awaited.append((verb, task))
        else:
            c.fire.append(task)
        metrics.mark_once("first_action_ms", c.ctx.elapsed_ms())

    def _reject(self, raw_action: dict, c: _Cycle, index: int, reason: str, errors: list[dict]) -> None:
        c.ctx.log.warning("rejected action", verb=raw_action.get("verb"), reason=reason)
        c.n_rejected += 1
        c.telemetry.rejected.append({"action_index": index, "action": raw_action, "errors": errors})
        c.span.add_event(EVENT_ACTION_REJECTED, {"verb": str(raw_action.get("verb")),
                                                 "reason": reason, tel.META_ACTION_INDEX: index})

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
