"""Text-mode end-to-end smoke of the SGR agent: real Nebius LLM, real catalog,
real memory lane and history — no speech keys needed.

Drives SGRAgentService with LLMContextFrames through a Pipecat test pipeline
and prints what would have been spoken, the commands that hit the bus, and
the per-turn marks. Reads OPENAI_API_KEY / OPENAI_BASE_URL from .env.

    uv run python tools/smoke_turn.py
    uv run python tools/smoke_turn.py --user couch_1 "something like Sicario but newer" "play the first one"
    uv run python tools/smoke_turn.py --user couch_1 --greet "yes, play it"   # returning viewer: greeting first
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from loguru import logger
from pipecat.frames.frames import (
    AggregatedTextFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.tests.utils import run_test
from pydantic_settings import SettingsConfigDict

from tv_avatar.agent.prompt import greeting_instruction
from tv_avatar.agent.service import SGRAgentService
from tv_avatar.agent.tools import InternalTools
from tv_avatar.config import Settings
from tv_avatar.control.bus import CommandBus
from tv_avatar.control.protocol import (
    CommandMsg,
    Playback,
    ScreenState,
    Tile,
)
from tv_avatar.logging import flush_logging, setup_logging
from tv_avatar.runtime import build_runtime
from tv_avatar.session.state import SessionState
from tv_avatar.tracing import session_scope, setup_tracing, shutdown_tracing

DEFAULT_TURNS = [
    "something like Sicario, but newer",
    "play the first one",
    "what did I just start watching?",
]


class SmokeSettings(Settings):
    """Speech keys are irrelevant here; everything else comes from .env."""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    slng_api_key: str = "-"
    anam_api_key: str = "-"
    anam_avatar_id: str = "-"
    agent_impl: str = "sgr"


class SimulatedTV:
    def __init__(self, bus: CommandBus, session: SessionState, titles: list[Tile],
                 *, recorder=None) -> None:
        self.bus, self.session, self.titles, self.recorder = bus, session, titles, recorder
        self.commands: list[CommandMsg] = []
        self.closing = False
        self.task: asyncio.Task | None = None

    async def __aenter__(self):
        self.task = asyncio.create_task(self._consume())
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.closing = True
        try:
            if exc_type is None:
                await self.task
        finally:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def _consume(self) -> None:
        while True:
            try:
                msg = await asyncio.wait_for(self.bus.next_outbound(), timeout=0.01)
            except TimeoutError:
                if self.closing:
                    return
                continue
            if not isinstance(msg, CommandMsg):
                continue
            self.commands.append(msg)
            self.bus.mark_sent(msg.id)
            if msg.verb == "search_catalog":
                query = msg.args["query"].casefold().strip()
                result = {"titles": [{"title_id": t.title_id, "name": t.name}
                                     for t in self.titles if query in t.name.casefold()]
                          [:msg.args.get("limit", 10)]}
                self.bus.record_reply(msg.id, "ok", {"ok": True}, reply_type="ack")
                self.bus.resolve(msg.id, result)
                self.bus.record_reply(msg.id, "ok", result, reply_type="result")
                continue
            old = self.session.screen
            if msg.verb == "play" and old is not None:
                self.session.update_screen(old.model_copy(update={
                    "view": "player", "playback": Playback(state="playing", title_id=msg.args["title_id"])}))
                if self.recorder is not None:
                    await self.recorder.on_screen_transition(self.session.user_id, old, self.session.screen)
            elif msg.verb == "show_titles" and old is not None:
                by_id = {t.title_id: t for t in self.titles}
                tiles = [by_id[tid].model_copy(update={"position": i})
                         for i, tid in enumerate(msg.args["title_ids"]) if tid in by_id]
                self.session.update_screen(old.model_copy(update={
                    "view": "grid", "tiles": tiles, "focus_index": 0 if tiles else None,
                    "rail_id": "agent:" + msg.args["label"]}))
            self.bus.record_reply(msg.id, "ok", {"ok": True}, reply_type="ack")


class Sink(FrameProcessor):
    def __init__(self) -> None:
        super().__init__()
        self.said: list[str] = []
        self.completed = False

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, AggregatedTextFrame):
            self.said.append(frame.text)
        elif isinstance(frame, LLMFullResponseEndFrame):
            self.completed = True
        await self.push_frame(frame, direction)


async def main(user_id: str, turns: list[str], *, greet: bool = False) -> int:
    settings = SmokeSettings()
    setup_logging(settings.log_level, settings=settings)
    traced = setup_tracing(settings)
    runtime = build_runtime(settings)
    if runtime.catalog is None:
        logger.error("catalog missing — run tools/build_catalog.py --limit 500 first")
        return 1
    await runtime.warm()  # E5 for the recs query embed + memory catch-up, as the app does at start

    session = SessionState("sess_smoke", "tok", 0, user_id=user_id)
    tiles = runtime.catalog.sample(4)
    sicario = runtime.catalog.lookup("273481")
    if sicario is not None:
        tiles = [sicario, *tiles[:3]]
    session.update_screen(ScreenState(
        view="grid", rail_id="home", focus_index=0,
        tiles=[Tile(title_id=t.title_id, name=t.name, position=i) for i, t in enumerate(tiles)],
        playback=Playback(state="stopped"),
    ))
    bus = CommandBus()
    context = LLMContext()
    tools = InternalTools(runtime.recs, runtime.catalog, recorder=runtime.recorder,
                          timeout_s=settings.tool_timeout_s)
    agent = SGRAgentService(settings, bus, runtime.lane, runtime.recs, runtime.history, session,
                            catalog=runtime.catalog, tools=tools, recorder=runtime.recorder)
    if traced:
        # No PipelineTask here, so no StartFrame carries enable_tracing and no turn
        # tracker parents the `llm` span: it is a root per turn, which is the
        # quickest way to inspect the agent subtree in isolation.
        original_setup = agent.setup

        async def traced_setup(cfg):
            await original_setup(cfg)
            agent._tracing_enabled = True

        agent.setup = traced_setup

    print(f"\nuser_id={user_id}  screen: " + " | ".join(t.label() for t in tiles) + "\n")
    if greet:
        turns = [greeting_instruction(session.persona.language), *turns]
    with session_scope(session, settings):
        await _run_turns(agent, bus, context, runtime, session, turns, user_id)
    profile = getattr(runtime.lane, "read_profile", lambda _u: "")(user_id)
    if profile:
        print("memory profile after this session:\n  " + profile.replace("\n", "\n  ") + "\n")
    await runtime.close()
    if traced:
        shutdown_tracing()
        print(f"traced {len(turns)} turn(s) for session_id={session.session_id}")
    return 0


async def _run_turns(agent, bus, context, runtime, session, turns, user_id) -> None:
    for text in turns:
        sink = Sink()
        context.add_message({"role": "user", "content": text})
        # What MemoryPrefetchTap does on the first STT partial: warm memory + the query vector.
        partial = text[:12]
        if runtime.recs is not None:
            runtime.recs.prefetch_query(user_id, text)
        await runtime.lane.prefetch(user_id, partial)
        t0 = time.perf_counter()
        titles = [Tile(title_id=t.title_id, name=t.name, position=i)
                  for i, t in enumerate(runtime.catalog.sample(len(runtime.catalog)))]
        async with asyncio.timeout(60), SimulatedTV(bus, session, titles, recorder=runtime.recorder) as tv:
            await run_test(Pipeline([agent, sink]), frames_to_send=[LLMContextFrame(context=context)],
                           expected_down_frames=None, start_timeout=5)
        elapsed = round((time.perf_counter() - t0) * 1000)
        said = " ".join(sink.said)
        context.add_message({"role": "assistant", "content": said})
        commands = tv.commands
        print(f"> {text}")
        print(f"  said: {said!r}")
        print(f"  commands: {[(c.verb, c.args) for c in commands]}   ({elapsed} ms)\n")
        # The agent ingests the turn itself at turn end (or on interruption).
    # What the pipeline runner does when the session ends: consolidate the transcript.
    await runtime.lane.finish_session(user_id)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default="couch_smoke")
    parser.add_argument("--greet", action="store_true",
                        help="open with the synthetic greeting turn, as the pipeline does on connect")
    parser.add_argument("turns", nargs="*", default=DEFAULT_TURNS)
    parser.add_argument("--routing-suite", action="store_true")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--max-requests", type=int, default=24)
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--ablate-context", action="store_true")
    args = parser.parse_args()
    if args.routing_suite:
        from tools.routing_eval import run_suite

        settings = SmokeSettings()
        setup_logging("INFO", settings=settings)
        try:
            exit_code = asyncio.run(run_suite(settings, repeat=args.repeat, max_requests=args.max_requests,
                                               case_ids=args.case_ids, ablate_context=args.ablate_context))
        finally:
            flush_logging()
        raise SystemExit(exit_code)
    raise SystemExit(asyncio.run(main(args.user, args.turns, greet=args.greet)))
