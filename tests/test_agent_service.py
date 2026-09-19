import asyncio
import time
from types import SimpleNamespace

import pytest
from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.tests.utils import SleepFrame, run_test

from tv_avatar.agent.service import SGRAgentService
from tv_avatar.agent.tools import InternalTools
from tv_avatar.config import Settings
from tv_avatar.control.bus import CommandBus
from tv_avatar.memory.fake import FakeMemoryLane
from tv_avatar.memory.lane import MemoryBlock
from tv_avatar.session.state import SessionState

PLAY = '{"intent":"control","say":"On it.","actions":[{"verb":"play","title_id":"1"}]}'
RECO_1 = '{"intent":"recommend","say":"Let me look.","actions":[{"verb":"recommend_titles","query":"heist"}]}'
RECO_2 = '{"intent":"recommend","say":"Try Heat or Inception.","actions":[{"verb":"focus","title_id":"949"}]}'


class FakeStream:
    def __init__(self, text: str, chunk: int = 7, delay_s: float = 0.0) -> None:
        self._parts = [text[i:i + chunk] for i in range(0, len(text), chunk)]
        self._delay = delay_s

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for part in self._parts:
            if self._delay:
                await asyncio.sleep(self._delay)
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=part), finish_reason=None)])


class FakeOpenAI:
    def __init__(self, scripts: list[str], delay_s: float = 0.0) -> None:
        self.scripts = list(scripts)
        self.calls: list[dict] = []
        self.delay_s = delay_s
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeStream(self.scripts.pop(0), delay_s=self.delay_s)


class RecordingBus(CommandBus):
    def __init__(self) -> None:
        super().__init__()
        self.first_dispatch_at: float | None = None

    async def dispatch(self, verb, args, turn_id):
        self.first_dispatch_at = self.first_dispatch_at or time.perf_counter()
        return await super().dispatch(verb, args, turn_id)


class TimingSink(FrameProcessor):
    def __init__(self) -> None:
        super().__init__()
        self.frames: list[Frame] = []
        self.first_text_at: float | None = None

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        self.frames.append(frame)
        if isinstance(frame, LLMTextFrame) and self.first_text_at is None:
            self.first_text_at = time.perf_counter()
        await self.push_frame(frame, direction)


class FakeTools(InternalTools):
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def run(self, verb, args, user_id, memory_text):
        self.calls.append((verb, args))
        return {"titles": [{"title_id": "949", "name": "Heat"}, {"title_id": "27205", "name": "Inception"}]}


def _settings() -> Settings:
    return Settings(slng_api_key="-", anam_api_key="-", anam_avatar_id="-", nebius_api_key="x", _env_file=None)


def _ctx(text: str) -> LLMContext:
    ctx = LLMContext()
    ctx.add_message({"role": "user", "content": text})
    return ctx


def _agent(client, bus, lane=None, tools=None):
    session = SessionState("sess_t", "tok", 0, user_id="u1")
    return SGRAgentService(_settings(), bus, lane or FakeMemoryLane(), None, None, session,
                           client=client, tools=tools or FakeTools())


async def _run(agent, sink, frames):
    return await run_test(Pipeline([agent, sink]), frames_to_send=frames, expected_down_frames=None,
                          start_timeout=5.0)


async def test_say_streams_as_llm_text_frames_before_actions_dispatch():
    bus, sink = RecordingBus(), TimingSink()
    agent = _agent(FakeOpenAI([PLAY]), bus)
    pushed_text_at: list[float] = []
    original_push = agent.push_frame

    async def timed_push(frame, direction=FrameDirection.DOWNSTREAM):
        if isinstance(frame, LLMTextFrame) and not pushed_text_at:
            pushed_text_at.append(time.perf_counter())
        await original_push(frame, direction)

    agent.push_frame = timed_push
    await _run(agent, sink, [LLMContextFrame(context=_ctx("play the first one"))])

    kinds = [type(f).__name__ for f in sink.frames
             if not type(f).__name__.startswith(("Start", "End", "LLMServiceMetadata"))]
    assert kinds[0] == "LLMFullResponseStartFrame"
    assert "LLMTextFrame" in kinds and kinds[-1] == "LLMFullResponseEndFrame"
    assert "".join(f.text for f in sink.frames if isinstance(f, LLMTextFrame)) == "On it."
    assert pushed_text_at[0] < bus.first_dispatch_at          # filler before action
    assert (await bus.next_outbound()).verb == "play"


async def test_awaited_action_triggers_second_cycle():
    bus, sink, tools = RecordingBus(), TimingSink(), FakeTools()
    client = FakeOpenAI([RECO_1, RECO_2])
    agent = _agent(client, bus, tools=tools)
    await _run(agent, sink, [LLMContextFrame(context=_ctx("recommend me a heist movie"))])

    assert [c[0] for c in tools.calls] == ["recommend_titles"]
    assert len(client.calls) == 2
    assert "Heat" in client.calls[1]["messages"][-1]["content"]      # results fed back
    said = "".join(f.text for f in sink.frames if isinstance(f, LLMTextFrame))
    assert said == "Let me look.Try Heat or Inception."
    assert sum(isinstance(f, LLMFullResponseStartFrame) for f in sink.frames) == 1
    assert sum(isinstance(f, LLMFullResponseEndFrame) for f in sink.frames) == 1
    assert (await bus.next_outbound()).verb == "focus"


async def test_cycles_are_capped_at_two():
    tools, client = FakeTools(), FakeOpenAI([RECO_1, RECO_1])
    agent = _agent(client, RecordingBus(), tools=tools)
    await _run(agent, TimingSink(), [LLMContextFrame(context=_ctx("recommend"))])
    assert len(client.calls) == 2
    assert len(tools.calls) == 1  # cycle-2 internal tools are not dispatched


async def test_interruption_frame_cancels_stream_and_queued_commands():
    bus, sink = RecordingBus(), TimingSink()
    slow = FakeOpenAI(['{"intent":"control","say":"Sure thing, one moment please.","actions":[{"verb":"home"},{"verb":"pause"}]}'],
                      delay_s=0.05)
    agent = _agent(slow, bus)
    await _run(agent, sink, [LLMContextFrame(context=_ctx("go home")), SleepFrame(0.12), InterruptionFrame()])

    assert any(isinstance(f, InterruptionFrame) for f in sink.frames)         # propagated, not swallowed
    assert not any(isinstance(f, LLMFullResponseEndFrame) for f in sink.frames)  # aborted turn does not end cleanly
    assert bus.pending_count() == 0
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(bus.next_outbound(), timeout=0.05)


async def test_system_prompt_carries_screen_memory_and_history():
    lane = FakeMemoryLane({"u1": MemoryBlock.from_lines(["hates horror"], [])})
    client = FakeOpenAI([PLAY])
    agent = _agent(client, RecordingBus(), lane=lane)
    await _run(agent, TimingSink(), [LLMContextFrame(context=_ctx("play"))])
    system = client.calls[0]["messages"][0]
    assert system["role"] == "system"
    assert "# Capabilities" in system["content"] and "# Screen" in system["content"]
    assert "hates horror" in system["content"] and "Recently watched" in system["content"]
    assert client.calls[0]["response_format"]["type"] == "json_schema"
    assert client.calls[0]["extra_body"] is None  # LLM_EXTRA_BODY={} → nothing sent


async def test_invalid_verb_args_are_rejected_not_raised():
    bus = RecordingBus()
    agent = _agent(FakeOpenAI(['{"intent":"control","say":"ok","actions":[{"verb":"seek","to_seconds":1,"delta_seconds":2}]}']), bus)
    await _run(agent, TimingSink(), [LLMContextFrame(context=_ctx("seek"))])
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(bus.next_outbound(), timeout=0.05)


async def test_turn_end_ingests_user_text_and_full_reply():
    lane = FakeMemoryLane()
    agent = _agent(FakeOpenAI([PLAY]), RecordingBus(), lane=lane)
    await _run(agent, TimingSink(), [LLMContextFrame(context=_ctx("play the first one"))])
    await asyncio.sleep(0.02)
    assert lane.ingests == [("u1", "play the first one", "On it.")]


async def test_interrupted_turn_still_ingests_user_text_with_partial_reply():
    lane = FakeMemoryLane()
    slow = FakeOpenAI(['{"intent":"chitchat","say":"I love that you love sci-fi, let me think about it some more.","actions":[]}'],
                      delay_s=0.05)  # 7-char chunks: say starts ~0.2 s in, ends ~0.6 s in
    agent = _agent(slow, RecordingBus(), lane=lane)
    await _run(agent, TimingSink(), [LLMContextFrame(context=_ctx("I love sci-fi")), SleepFrame(0.4), InterruptionFrame()])
    await asyncio.sleep(0.02)
    assert len(lane.ingests) == 1
    user_id, user_text, said = lane.ingests[0]
    assert (user_id, user_text) == ("u1", "I love sci-fi")
    assert 0 < len(said) < len("I love that you love sci-fi, let me think about it some more.")


async def test_greeting_instruction_is_never_ingested():
    from tv_avatar.agent.prompt import GREETING_INSTRUCTION
    lane = FakeMemoryLane()
    agent = _agent(FakeOpenAI(['{"intent":"chitchat","say":"Hi!","actions":[]}']), RecordingBus(), lane=lane)
    await _run(agent, TimingSink(), [LLMContextFrame(context=_ctx(GREETING_INSTRUCTION))])
    await asyncio.sleep(0.02)
    assert lane.ingests == []


async def test_slow_llm_gets_a_spoken_filler():
    settings = _settings().model_copy(update={"filler_after_ms": 100})
    session = SessionState("sess_t", "tok", 0, user_id="u1")
    # First content chunk only after ~0.35 s: filler must fire, then the real say.
    slow = FakeOpenAI(['{"intent":"chitchat","say":"Here you go.","actions":[]}'], delay_s=0.35)
    sink = TimingSink()
    agent = SGRAgentService(settings, RecordingBus(), FakeMemoryLane(), None, None, session,
                            client=slow, tools=FakeTools())
    await _run(agent, sink, [LLMContextFrame(context=_ctx("hello"))])
    kinds = [type(f).__name__ for f in sink.frames]
    assert "TTSSpeakFrame" in kinds
    assert kinds.index("TTSSpeakFrame") < kinds.index("LLMTextFrame")
    assert "".join(f.text for f in sink.frames if isinstance(f, LLMTextFrame)) == "Here you go."


async def test_fast_llm_gets_no_filler():
    settings = _settings().model_copy(update={"filler_after_ms": 500})
    session = SessionState("sess_t", "tok", 0, user_id="u1")
    sink = TimingSink()
    agent = SGRAgentService(settings, RecordingBus(), FakeMemoryLane(), None, None, session,
                            client=FakeOpenAI([PLAY]), tools=FakeTools())
    await _run(agent, sink, [LLMContextFrame(context=_ctx("play"))])
    assert not any(type(f).__name__ == "TTSSpeakFrame" for f in sink.frames)
