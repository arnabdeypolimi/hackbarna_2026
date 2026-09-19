"""Text-mode end-to-end smoke of the SGR agent: real Nebius LLM, real catalog,
real memory lane and history — no speech keys needed.

Drives SGRAgentService with LLMContextFrames through a Pipecat test pipeline
and prints what would have been spoken, the commands that hit the bus, and
the per-turn marks. Reads OPENAI_API_KEY / OPENAI_BASE_URL from .env.

    uv run python tools/smoke_turn.py
    uv run python tools/smoke_turn.py --user couch_1 "something like Sicario but newer" "play the first one"
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from loguru import logger
from pipecat.frames.frames import AggregatedTextFrame, LLMContextFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.tests.utils import run_test
from pydantic_settings import SettingsConfigDict

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
from tv_avatar.logging import setup_logging
from tv_avatar.runtime import build_runtime
from tv_avatar.session.state import SessionState

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


class Sink(FrameProcessor):
    def __init__(self) -> None:
        super().__init__()
        self.said: list[str] = []

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, AggregatedTextFrame):
            self.said.append(frame.text)
        await self.push_frame(frame, direction)


async def main(user_id: str, turns: list[str]) -> int:
    settings = SmokeSettings()
    setup_logging(settings.log_level)
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
    tools = InternalTools(runtime.recs, runtime.lane, runtime.catalog, recorder=runtime.recorder,
                          timeout_s=settings.tool_timeout_s)
    agent = SGRAgentService(settings, bus, runtime.lane, runtime.recs, runtime.history, session,
                            catalog=runtime.catalog, tools=tools, recorder=runtime.recorder)

    print(f"\nuser_id={user_id}  screen: " + " | ".join(t.label() for t in tiles) + "\n")
    for text in turns:
        sink = Sink()
        context.add_message({"role": "user", "content": text})
        # What MemoryPrefetchTap does on the first STT partial: warm memory + the query vector.
        partial = text[:12]
        if runtime.recs is not None:
            runtime.recs.prefetch_query(user_id, text)
        await runtime.lane.prefetch(user_id, partial)
        t0 = time.perf_counter()
        await run_test(Pipeline([agent, sink]), frames_to_send=[LLMContextFrame(context=context)],
                       expected_down_frames=None, start_timeout=5)
        elapsed = round((time.perf_counter() - t0) * 1000)
        said = " ".join(sink.said)
        context.add_message({"role": "assistant", "content": said})
        commands: list[CommandMsg] = []
        while True:
            try:
                msg = await asyncio.wait_for(bus.next_outbound(), timeout=0.05)
            except TimeoutError:
                break
            if isinstance(msg, CommandMsg):
                commands.append(msg)
                if msg.verb == "play":
                    session.update_screen(session.screen.model_copy(update={
                        "view": "player", "playback": Playback(state="playing", title_id=msg.args["title_id"])}))
                    await runtime.recorder.on_screen_transition(user_id, None, session.screen)
        print(f"> {text}")
        print(f"  said: {said!r}")
        print(f"  commands: {[(c.verb, c.args) for c in commands]}   ({elapsed} ms)\n")
        await runtime.lane.ingest_turn(user_id, text, said)
    # What the pipeline runner does when the session ends: consolidate the transcript.
    await runtime.lane.finish_session(user_id)
    profile = getattr(runtime.lane, "read_profile", lambda _u: "")(user_id)
    if profile:
        print("memory profile after this session:\n  " + profile.replace("\n", "\n  ") + "\n")
    await runtime.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default="couch_smoke")
    parser.add_argument("turns", nargs="*", default=DEFAULT_TURNS)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.user, args.turns)))
