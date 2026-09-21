import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

from loguru import logger
from openai import AsyncOpenAI
from pipecat.frames.frames import LLMContextFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.tests.utils import run_test
from pydantic import BaseModel, Field

from tv_avatar.agent.envelope import FinalTurnPlan, TurnPlan
from tv_avatar.agent.loop import MAX_TOKENS, TEMPERATURE
from tv_avatar.agent.prompt import build_system_prompt
from tv_avatar.agent.service import SGRAgentService
from tv_avatar.catalog import AvatarProfile, LanguageProfile, SessionPersona
from tv_avatar.config import Settings
from tv_avatar.control.bus import CommandBus
from tv_avatar.control.protocol import Playback, ScreenState, Tile
from tv_avatar.memory.fake import FakeMemoryLane
from tv_avatar.memory.lane import MemoryBlock
from tv_avatar.recs.catalog import CatalogItem
from tv_avatar.session.state import SessionState

FIXTURES = Path(__file__).resolve().parents[1] / "tests/fixtures/named_title_routing.json"
PERSONA = SessionPersona(
    avatar=AvatarProfile(id="eval", name="Eval", anam_avatar_id="unused", voice="unused"),
    language=LanguageProfile(code="en", name="English", native_name="English"))


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class RoutingCase(BaseModel):
    id: str
    request: str
    visible_ids: list[str]
    sequences: list[list[str]]
    #: The operation every cycle of the turn must decode from `request` — the
    #: routing decision itself, scored apart from the actions it licenses.
    operation: str | None = None
    target_id: str | None = None
    search_query: str | None = None
    final_intent: str | None = None
    recommendation_ids: list[str] = Field(default_factory=list)
    profile: str = "clean"
    history: str = "clean"


class RoutingSuite(BaseModel):
    catalog: list[CatalogItem]
    profiles: dict[str, str]
    histories: dict[str, list[Message]]
    cases: list[RoutingCase]


@dataclass
class RequestBudget:
    limit: int
    used: int = 0

    def consume(self) -> None:
        if self.used >= self.limit:
            raise RuntimeError("evaluation request budget exhausted")
        self.used += 1


class RecordedStream:
    def __init__(self, stream: Any, record: dict) -> None:
        self.stream, self.record = stream, record

    async def __aiter__(self):
        async for chunk in self.stream:
            if chunk.choices and chunk.choices[0].delta:
                self.record["output"] += chunk.choices[0].delta.content or ""
            yield chunk

    async def close(self) -> None:
        close = getattr(self.stream, "close", None)
        if close is not None:
            await close()


class RecordedClient:
    def __init__(self, client: Any, budget: RequestBudget) -> None:
        self.client, self.budget = client, budget
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        self.budget.consume()
        record = {"messages": kwargs["messages"], "output": "",
                  "schema": kwargs["response_format"]["json_schema"]["name"]}
        self.calls.append(record)
        return RecordedStream(await self.client.chat.completions.create(**kwargs), record)


class FixtureCatalog:
    def __init__(self, titles: list[CatalogItem]) -> None:
        self.titles = {t.title_id: t for t in titles}

    def lookup(self, title_id: str) -> CatalogItem | None:
        return self.titles.get(title_id)


class FixtureTools:
    def __init__(self, titles: list[CatalogItem]) -> None:
        self.titles = titles

    async def run(self, action, user_id: str) -> dict:
        if action.verb == "recommend_titles":
            return {"titles": [t.model_dump(include={"title_id", "name", "year", "genres"})
                               for t in self.titles], "matched": bool(self.titles)}
        return {"status": "ok"}


def score(case: RoutingCase, calls: list[dict], commands: list[dict], *, known_ids: set[str]) -> list[str]:
    failures: list[str] = []
    plans = []
    for call in calls:
        model = FinalTurnPlan if call["schema"] == "turn_plan_final" else TurnPlan
        try:
            plans.append(model.model_validate_json(call["output"]))
        except ValueError:
            failures.append("invalid or incomplete model plan")
    actions = [a.model_dump(exclude_none=True) for p in plans for a in p.actions]
    verbs = [str(a["verb"]) for a in actions]
    referenced = {a[k] for a in actions for k in ("title_id", "similar_to") if a.get(k)}
    referenced.update(tid for a in actions for tid in a.get("title_ids", []))
    if referenced - known_ids:
        failures.append(f"unknown title ids: {sorted(referenced - known_ids)}")
    if not plans:
        failures.append("no model plan")
    if case.operation is not None:
        operations = [p.request.operation for p in plans]
        if set(operations) != {case.operation}:
            failures.append(f"decoded operation {operations}, expected {case.operation}")
    ids = {p.request.title_id for p in plans if p.request.title_id}
    if ids - known_ids:
        failures.append(f"request decoded an unknown title id: {sorted(ids - known_ids)}")
    if verbs not in case.sequences:
        failures.append(f"unexpected action sequence: {verbs}")
    tv_actions = [{"verb": a["verb"], "args": {k: v for k, v in a.items() if k != "verb"}}
                  for a in actions if a["verb"] not in {"recommend_titles", "reject_title"}]
    if tv_actions != commands:
        failures.append("TV dispatch differs from model plan")
    if case.search_query is not None:
        queries = [a["query"].strip().casefold() for a in actions if a["verb"] == "search_catalog"]
        if queries != [case.search_query.casefold()]:
            failures.append(f"wrong search query: {queries}")
    if case.target_id is not None:
        targets = [a.get("title_id") for a in actions if "title_id" in a]
        targets += [tid for a in actions for tid in a.get("title_ids", [])]
        # Every title-directed action points at the requested movie; how many
        # of them the model used is the sequence's business, not the target's.
        if set(targets) != {case.target_id}:
            failures.append(f"wrong target: {targets}")
    if case.recommendation_ids:
        shown = [tid for a in actions for tid in a.get("title_ids", [])]
        if shown != case.recommendation_ids:
            failures.append(f"wrong recommendation rail: {shown}")
    if case.final_intent and plans and plans[-1].intent != case.final_intent:
        failures.append(f"expected final intent {case.final_intent}")
    return failures


async def evaluate(case: RoutingCase, suite: RoutingSuite, settings: Settings, client: Any,
                   budget: RequestBudget, *, timeout_s: float = 30) -> dict:
    from tools.smoke_turn import SimulatedTV, Sink

    catalog = FixtureCatalog(suite.catalog)
    session = SessionState("sess_routing_" + case.id, "unused", 0, user_id="routing_eval", persona=PERSONA)
    tiles = [Tile(title_id=t.title_id, name=t.name, position=i)
             for i, t in enumerate(suite.catalog)]
    visible = [Tile(title_id=tid, name=catalog.lookup(tid).name, position=i)
               for i, tid in enumerate(case.visible_ids)]
    session.update_screen(ScreenState(view="grid", rail_id="eval", focus_index=0 if visible else None,
                                      tiles=visible, playback=Playback(state="stopped")))
    memory = FakeMemoryLane({session.user_id: MemoryBlock(profile=suite.profiles[case.profile])})
    recorded = RecordedClient(client, budget)
    bus, sink = CommandBus(), Sink()
    tools = FixtureTools([catalog.lookup(tid) for tid in case.recommendation_ids])
    agent = SGRAgentService(settings, bus, memory, None, None, session, catalog=catalog,
                            client=recorded, tools=tools)
    context = LLMContext([m.model_dump() for m in suite.histories[case.history]] + [
        {"role": "user", "content": case.request}])
    tv = SimulatedTV(bus, session, tiles)
    started = time.perf_counter()
    error = None
    try:
        async with asyncio.timeout(timeout_s), tv:
            await run_test(Pipeline([agent, sink]), frames_to_send=[LLMContextFrame(context=context)],
                           expected_down_frames=None, start_timeout=5)
    except (TimeoutError, RuntimeError, ValueError) as err:
        error = type(err).__name__
    commands = [{"verb": c.verb, "args": c.args} for c in tv.commands]
    failures = score(case, recorded.calls, commands, known_ids=set(catalog.titles))
    if error:
        failures.append(error)
    if not sink.completed:
        failures.append("turn did not complete")
    return {"case": case.id, "passed": not failures, "failures": failures,
            "commands": commands, "speech": sink.said, "calls": recorded.calls,
            "ms": round((time.perf_counter() - started) * 1000)}


async def run_suite(settings: Settings, *, repeat: int, max_requests: int,
                    case_ids: list[str] | None = None, ablate_context: bool = False) -> int:
    suite = RoutingSuite.model_validate_json(FIXTURES.read_text())
    cases = [c for c in suite.cases if case_ids is None or c.id in case_ids]
    if not cases or (case_ids and set(case_ids) - {c.id for c in cases}):
        raise ValueError("unknown or empty routing case selection")
    if repeat < 1 or max_requests < 1:
        raise ValueError("repeat and max_requests must be positive")
    if ablate_context:
        if case_ids is None:
            raise ValueError("context ablation requires explicit --case selection")
        cases = [case.model_copy(update={"id": f"{case.id}_{profile}_{history}",
                                        "profile": profile, "history": history})
                 for case in cases for profile in ("clean", "biased") for history in ("clean", "biased")]
    settings = settings.model_copy(update={"agent_impl": "sgr", "agent_max_cycles": 2,
                                            "tracing_enabled": False})
    budget = RequestBudget(max_requests)
    logger.info("routing.eval.config {}", json.dumps({"model": settings.llm_model,
        "temperature": TEMPERATURE, "max_tokens": MAX_TOKENS, "repeat": repeat,
        "max_requests": max_requests, "cycle_first_byte_s": settings.cycle_first_byte_s,
        "prompt_hash": hashlib.sha256(build_system_prompt(PERSONA.language).encode()).hexdigest()}))
    results = []
    async with AsyncOpenAI(api_key=settings.nebius_api_key, base_url=settings.nebius_base_url,
                           max_retries=0, timeout=25) as client:
        for repetition in range(repeat):
            for case in cases:
                if budget.used >= budget.limit:
                    logger.error("routing.eval.budget_exhausted used={}", budget.used)
                    return 2
                result = await evaluate(case, suite, settings, client, budget)
                result["repetition"] = repetition + 1
                results.append(result)
                report = {**result, "request": case.request, "profile": case.profile, "history": case.history,
                          "calls": [{k: c[k] for k in ("schema", "output")} for c in result["calls"]]}
                logger.info("routing.eval.result {}", json.dumps(report, ensure_ascii=False))
    passed = sum(r["passed"] for r in results)
    logger.info("routing.eval.summary passed={} total={} requests={}", passed, len(results), budget.used)
    return 0 if passed == len(results) else 1
