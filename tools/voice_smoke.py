"""Voice-API smoke of the agent — no browser, no mic.

Each scripted user utterance is synthesised with SLNG TTS (a different
voice), streamed in real time as microphone audio into the *real* pipeline
(Reson8 STT → prefetch tap → user aggregator → injector → SGR agent →
Cartesia TTS), and the agent's spoken reply, TV commands and per-stage
latencies are printed. Anam and WebRTC are left out; everything else is
the production path.

    uv run python tools/voice_smoke.py
    uv run python tools/voice_smoke.py --user couch_1 "I love Batman films" "what do I love?"
"""
import argparse
import asyncio
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    LLMContextFrame,
    LLMRunFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSTextFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.tests.utils import SleepFrame, run_test

from tv_avatar.agent.injector import ScreenContextInjector
from tv_avatar.agent.prompt import initial_messages
from tv_avatar.config import Settings
from tv_avatar.control.bus import CommandBus
from tv_avatar.control.protocol import CommandMsg, Playback, ScreenState, Tile
from tv_avatar.logging import setup_logging
from tv_avatar.memory.taps import MemoryPrefetchTap
from tv_avatar.pipeline.builder import build_agent
from tv_avatar.pipeline.services import build_stt, build_tts
from tv_avatar.pipeline.turns import user_aggregator_params
from tv_avatar.runtime import build_runtime
from tv_avatar.session.state import SessionState

DEFAULT_TURNS = [
    "Something like Sicario, but newer.",
    "Play the first one.",
    "What did I just start watching?",
]
#: A voice other than the avatar's so the STT hears a different speaker.
USER_VOICE = "a0e99841-438c-4a64-b679-ae501e7d6091"
CHUNK_MS = 20
TRAILING_SILENCE_S = 1.5
REPLY_WAIT_S = 9.0


class AudioCollector(FrameProcessor):
    def __init__(self) -> None:
        super().__init__()
        self.audio = bytearray()
        self.sample_rate = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSAudioRawFrame):
            self.audio += frame.audio
            self.sample_rate = frame.sample_rate
        await self.push_frame(frame, direction)


class Stats:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.t_stop: float | None = None
        self.t_transcript: float | None = None
        self.t_context: float | None = None
        self.t_first_text: float | None = None
        self.t_first_audio: float | None = None
        self.transcript = ""
        self.said: list[str] = []
        self.audio_bytes = 0


class Tap(FrameProcessor):
    """Records timing marks for the frames that pass this point of the pipeline."""

    def __init__(self, stats: Stats) -> None:
        super().__init__()
        self._s = stats

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        s, now = self._s, time.perf_counter()
        if isinstance(frame, UserStoppedSpeakingFrame):
            s.t_stop = s.t_stop or now
        elif isinstance(frame, TranscriptionFrame):
            s.t_transcript = s.t_transcript or now
            s.transcript = (s.transcript + " " + frame.text).strip()
        elif isinstance(frame, LLMContextFrame):
            s.t_context = s.t_context or now
        elif isinstance(frame, LLMTextFrame):
            s.t_first_text = s.t_first_text or now
        elif isinstance(frame, TTSTextFrame):
            s.said.append(frame.text)
        elif isinstance(frame, TTSAudioRawFrame):
            s.t_first_audio = s.t_first_audio or now
            s.audio_bytes += len(frame.audio)
        await self.push_frame(frame, direction)


class SmokeSettings(Settings):
    """Anam is not exercised here; everything else comes from .env."""
    anam_api_key: str = "-"
    anam_avatar_id: str = "-"
    agent_impl: str = "sgr"


MIC_RATE = 16000  # what the WebRTC input transport delivers; Silero VAD requires 16 k or 8 k


async def synthesize(settings: Settings, text: str) -> tuple[bytes, int]:
    tts = build_tts(settings.model_copy(update={"slng_tts_voice": USER_VOICE}))
    collector = AudioCollector()
    await run_test(Pipeline([tts, collector]), frames_to_send=[TTSSpeakFrame(text), SleepFrame(4.0)],
                   expected_down_frames=None, start_timeout=10)
    return _resample(bytes(collector.audio), collector.sample_rate, MIC_RATE), MIC_RATE


def _resample(pcm16: bytes, src: int, dst: int) -> bytes:
    if src == dst or not pcm16:
        return pcm16
    import numpy as np
    x = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32)
    n = int(len(x) * dst / src)
    y = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    return y.astype(np.int16).tobytes()


def _audio_frames(audio: bytes, sample_rate: int) -> list:
    step = sample_rate * 2 * CHUNK_MS // 1000
    frames: list = []
    for i in range(0, len(audio), step):
        frames.append(InputAudioRawFrame(audio=audio[i:i + step], sample_rate=sample_rate, num_channels=1))
        frames.append(SleepFrame(CHUNK_MS / 1000))
    silence = bytes(step)
    for _ in range(int(TRAILING_SILENCE_S * 1000 / CHUNK_MS)):
        frames.append(InputAudioRawFrame(audio=silence, sample_rate=sample_rate, num_channels=1))
        frames.append(SleepFrame(CHUNK_MS / 1000))
    return frames


def _ms(a: float | None, b: float | None) -> str:
    return f"{round((b - a) * 1000):>5} ms" if a and b else "    —   "


async def main(user_id: str, turns: list[str]) -> int:
    settings = SmokeSettings()
    setup_logging(settings.log_level)
    # Embedded Qdrant is single-process: a running uvicorn holds data/qdrant_db,
    # so the harness works on a throw-away copy of the index.
    if Path(settings.qdrant_path).exists():
        scratch = Path(tempfile.mkdtemp(prefix="qdrant_smoke_"))
        shutil.copytree(settings.qdrant_path, scratch / "qdrant_db")
        (scratch / "qdrant_db" / ".lock").unlink(missing_ok=True)
        settings = settings.model_copy(update={"qdrant_path": str(scratch / "qdrant_db")})
    runtime = build_runtime(settings)
    await runtime.lane.warmup()

    logger.info("synthesising {} user utterances with SLNG TTS", len(turns))
    utterances = [await synthesize(settings, text) for text in turns]
    sample_rate = utterances[0][1]

    session = SessionState("sess_voice", "tok", 0, user_id=user_id)
    tiles = runtime.catalog.sample(4) if runtime.catalog else []
    session.update_screen(ScreenState(
        view="grid", rail_id="home", focus_index=0,
        tiles=[Tile(title_id=t.title_id, name=t.name, position=i) for i, t in enumerate(tiles)],
        playback=Playback(state="stopped"),
    ))
    bus = CommandBus()
    context = LLMContext(messages=initial_messages())
    user_agg, assistant_agg = LLMContextAggregatorPair(
        context, user_params=user_aggregator_params(turn_silence_s=settings.turn_silence_s))
    reply = Stats()
    pipeline = Pipeline([
        build_stt(settings),
        Tap(reply),                      # transcripts, user-stopped
        MemoryPrefetchTap(runtime.lane, session, min_chars=settings.mem_prefetch_min_chars, recs=runtime.recs),
        user_agg,
        Tap(reply),                      # LLMContextFrame (turn opens)
        ScreenContextInjector(session, catalog=runtime.catalog, history=runtime.history),
        build_agent(settings, runtime, session, bus),
        Tap(reply),                      # first say byte (LLMTextFrame)
        build_tts(settings),
        Tap(reply),                      # spoken text, first audio
        assistant_agg,
    ])

    # Let the STT/TTS websockets come up before the first word (Reson8 "session
    # ready" lands ~1 s after StartFrame; audio before that is lost).
    # Like the browser session: the avatar greets first (LLMRunFrame), so the
    # user's first utterance is never the pipeline's first turn.
    frames: list = [SleepFrame(2.0), LLMRunFrame(), SleepFrame(6.0)]
    for audio, _rate in utterances:
        frames += _audio_frames(audio, sample_rate)
        frames.append(SleepFrame(REPLY_WAIT_S))

    print(f"\nuser_id={user_id}  screen: " + " | ".join(t.label() for t in tiles))
    print("streaming audio into STT → agent → TTS …\n")

    results: list[dict] = []

    # run_test drives the whole script; we snapshot per utterance by wall-clock.
    async def snapshot_loop() -> None:
        per_utt = [len(_audio_frames(a, sample_rate)) // 2 * CHUNK_MS / 1000 + REPLY_WAIT_S for a, _ in utterances]
        await asyncio.sleep(8.0)
        reply.reset()
        for text, dur in zip(turns, per_utt, strict=True):
            await asyncio.sleep(dur)
            commands: list[CommandMsg] = []
            while True:
                try:
                    msg = await asyncio.wait_for(bus.next_outbound(), timeout=0.02)
                except TimeoutError:
                    break
                if isinstance(msg, CommandMsg):
                    commands.append(msg)
            results.append({
                "asked": text, "heard": reply.transcript, "said": " ".join(reply.said).strip(),
                "commands": [(c.verb, c.args) for c in commands],
                "stop→transcript": _ms(reply.t_stop, reply.t_transcript),
                "transcript→context": _ms(reply.t_transcript, reply.t_context),
                "context→first text": _ms(reply.t_context, reply.t_first_text),
                "first text→first audio": _ms(reply.t_first_text, reply.t_first_audio),
                "stop→first audio": _ms(reply.t_stop, reply.t_first_audio),
                "audio_s": round(reply.audio_bytes / (2 * 24000), 1),
            })
            reply.reset()

    snap = asyncio.create_task(snapshot_loop())
    await run_test(pipeline, frames_to_send=frames, expected_down_frames=None, start_timeout=15,
                   pipeline_params=PipelineParams(enable_metrics=True, audio_in_sample_rate=sample_rate,
                                                  audio_out_sample_rate=settings.slng_tts_sample_rate))
    await snap

    for r in results:
        print(f"> asked : {r['asked']}")
        print(f"  heard : {r['heard']!r}")
        print(f"  said  : {r['said']!r}  ({r['audio_s']} s of audio)")
        print(f"  cmds  : {r['commands']}")
        print("  timing: " + "  ".join(f"{k}={v.strip()}" for k, v in r.items()
                                       if k.endswith(("transcript", "context", "text", "audio")) and k != "audio_s"))
        print()
    await runtime.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default="couch_voice")
    parser.add_argument("turns", nargs="*", default=DEFAULT_TURNS)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.user, args.turns)))
