import asyncio
import time
from types import SimpleNamespace

import pytest
from pipecat.frames.frames import (
    AggregatedTextFrame,
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
        if isinstance(frame, AggregatedTextFrame) and self.first_text_at is None:
            self.first_text_at = time.perf_counter()
        await self.push_frame(frame, direction)


def _spoken(sink: TimingSink) -> list[str]:
    """Sentences handed to TTS, in order. The agent never streams raw
    LLMTextFrames: the TTS aggregator's lookahead would hold a cycle's last
    sentence until the next cycle's text arrived."""
    assert not any(isinstance(f, LLMTextFrame) for f in sink.frames)
    return [f.text for f in sink.frames if isinstance(f, AggregatedTextFrame)]


class FakeTools(InternalTools):
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.first_call_at: float | None = None

    async def run(self, action, user_id):
        self.first_call_at = self.first_call_at or time.perf_counter()
        self.calls.append((str(action.verb), action.model_dump(exclude={"verb"}, exclude_none=True)))
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


def _time_first_speech(agent) -> list[float]:
    """When the agent *pushed* its first sentence (the sink sees it a queue hop later)."""
    pushed_text_at: list[float] = []
    original_push = agent.push_frame

    async def timed_push(frame, direction=FrameDirection.DOWNSTREAM):
        if isinstance(frame, AggregatedTextFrame) and not pushed_text_at:
            pushed_text_at.append(time.perf_counter())
        await original_push(frame, direction)

    agent.push_frame = timed_push
    return pushed_text_at


async def test_say_reaches_tts_as_sentences_before_actions_dispatch():
    bus, sink = RecordingBus(), TimingSink()
    agent = _agent(FakeOpenAI([PLAY]), bus)
    pushed_text_at = _time_first_speech(agent)
    await _run(agent, sink, [LLMContextFrame(context=_ctx("play the first one"))])

    kinds = [type(f).__name__ for f in sink.frames
             if not type(f).__name__.startswith(("Start", "End", "LLMServiceMetadata"))]
    assert kinds[0] == "LLMFullResponseStartFrame"
    assert "AggregatedTextFrame" in kinds and kinds[-1] == "LLMFullResponseEndFrame"
    assert _spoken(sink) == ["On it."]
    assert pushed_text_at[0] < bus.first_dispatch_at          # filler before action
    assert (await bus.next_outbound()).verb == "play"


async def test_multi_sentence_say_is_split_and_streamed_per_sentence():
    sink = TimingSink()
    agent = _agent(FakeOpenAI(['{"intent":"chitchat","say":"Sure thing. Rainy, slow and sad it is","actions":[]}']),
                   RecordingBus())
    await _run(agent, sink, [LLMContextFrame(context=_ctx("something depressing"))])
    # The trailing fragment has no terminal punctuation: it must still be spoken.
    assert _spoken(sink) == ["Sure thing.", "Rainy, slow and sad it is"]


async def test_awaited_action_triggers_second_cycle():
    bus, sink, tools = RecordingBus(), TimingSink(), FakeTools()
    client = FakeOpenAI([RECO_1, RECO_2])
    agent = _agent(client, bus, tools=tools)
    pushed_text_at = _time_first_speech(agent)
    await _run(agent, sink, [LLMContextFrame(context=_ctx("recommend me a heist movie"))])

    assert [c[0] for c in tools.calls] == ["recommend_titles"]
    assert len(client.calls) == 2
    assert "Heat" in client.calls[1]["messages"][-1]["content"]      # results fed back
    # Two sentences, two frames: the filler is spoken while the tool runs and
    # the answer is never glued to it ("Let me look.Try Heat...").
    assert _spoken(sink) == ["Let me look.", "Try Heat or Inception."]
    assert pushed_text_at[0] < tools.first_call_at
    assert sum(isinstance(f, LLMFullResponseStartFrame) for f in sink.frames) == 1
    assert sum(isinstance(f, LLMFullResponseEndFrame) for f in sink.frames) == 1
    assert (await bus.next_outbound()).verb == "focus"


@pytest.mark.parametrize("max_cycles", [1, 2, 3])
async def test_cycles_are_capped_at_setting(max_cycles):
    """The model asks for a tool on every cycle; the loop still ends at AGENT_MAX_CYCLES
    and the last cycle's tool call is refused rather than run without a reply."""
    settings = _settings().model_copy(update={"agent_max_cycles": max_cycles})
    tools, client = FakeTools(), FakeOpenAI([RECO_1] * 4)
    agent = SGRAgentService(settings, RecordingBus(), FakeMemoryLane(), None, None,
                            SessionState("sess_t", "tok", 0, user_id="u1"), client=client, tools=tools)
    sink = TimingSink()
    await _run(agent, sink, [LLMContextFrame(context=_ctx("recommend"))])
    assert len(client.calls) == max_cycles
    assert len(tools.calls) == max_cycles - 1      # the final cycle's internal tool is not dispatched
    assert _spoken(sink) == ["Let me look."] * max_cycles
    assert sum(isinstance(f, LLMFullResponseEndFrame) for f in sink.frames) == 1


async def test_feedback_tells_the_model_when_tools_are_still_allowed():
    settings = _settings().model_copy(update={"agent_max_cycles": 3})
    client = FakeOpenAI([RECO_1, RECO_1, RECO_2])
    agent = SGRAgentService(settings, RecordingBus(), FakeMemoryLane(), None, None,
                            SessionState("sess_t", "tok", 0, user_id="u1"), client=client, tools=FakeTools())
    await _run(agent, TimingSink(), [LLMContextFrame(context=_ctx("recommend"))])
    assert len(client.calls) == 3
    second, third = client.calls[1]["messages"][-1]["content"], client.calls[2]["messages"][-1]["content"]
    assert second.startswith("[tool results]") and third.startswith("[tool results]")
    assert "Do not call internal tools again" not in second and "one more internal tool" in second
    assert "Do not call internal tools again" in third
    # Every follow-up cycle carries the whole exchange so far: envelope, results, envelope, results.
    assert [m["role"] for m in client.calls[2]["messages"][-4:]] == ["assistant", "user", "assistant", "user"]


async def test_every_follow_up_cycle_is_budgeted():
    """With a 3-cycle cap, a slow cycle 2 still falls back after ONE budget — no cycle 3."""
    class TwoSpeeds(FakeOpenAI):
        async def _create(self, **kwargs):
            self.calls.append(kwargs)
            return FakeStream(self.scripts.pop(0), delay_s=0.0 if len(self.calls) == 1 else 0.3)

    settings = _settings().model_copy(update={"agent_max_cycles": 3, "cycle_first_byte_s": 0.15})
    client, sink, bus = TwoSpeeds([RECO_1, RECO_1, RECO_2]), TimingSink(), RecordingBus()
    agent = SGRAgentService(settings, bus, FakeMemoryLane(), None, None,
                            SessionState("sess_t", "tok", 0, user_id="u1"), client=client, tools=FakeTools())
    await _run(agent, sink, [LLMContextFrame(context=_ctx("recommend"))])
    assert len(client.calls) == 2
    assert _spoken(sink) == ["Let me look.", "How about Heat, or Inception?"]
    assert (await bus.next_outbound()).verb == "focus"


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
    assert client.calls[0]["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}


def _sections(system: str) -> list[str]:
    return [line for line in system.splitlines() if line.startswith("# ")]


@pytest.mark.parametrize("incoming_system", [
    None,
    "whatever",
    "stale persona\n\n# Screen\nView: old\n\n# Recent activity\nRecently watched: Old Film (id=1)",
])
async def test_agent_is_the_only_writer_of_the_system_prompt(incoming_system):
    """No sniffing of what arrived: the system message is rebuilt from scratch every
    turn, so every section appears exactly once whether or not something upstream
    (an injector, a stale persona, a previous turn) already stamped one."""
    from tv_avatar.control.protocol import Playback, ScreenState, Tile

    lane = FakeMemoryLane({"u1": MemoryBlock.from_lines(["hates horror"], [])})
    client = FakeOpenAI([PLAY])
    agent = _agent(client, RecordingBus(), lane=lane)
    agent._session.update_screen(ScreenState(
        view="grid", focus_index=0, tiles=[Tile(title_id="27205", name="Inception", position=0)],
        playback=Playback(state="stopped")))
    ctx = LLMContext()
    if incoming_system is not None:
        ctx.add_message({"role": "system", "content": incoming_system})
    ctx.add_message({"role": "user", "content": "play the first one"})
    await _run(agent, TimingSink(), [LLMContextFrame(context=ctx)])

    messages = client.calls[0]["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]
    system = messages[0]["content"]
    assert "whatever" not in system and "stale" not in system and "Old Film" not in system
    heads = _sections(system)
    for section in ("# Capabilities", "# Screen", "# Memory", "# Recent activity"):
        assert heads.count(section) == 1, heads
    assert heads.index("# Screen") < heads.index("# Memory") < heads.index("# Recent activity")
    assert "Inception (id=27205) <- focused" in system   # the live SessionState, not the incoming text
    assert "hates horror" in system


async def test_invalid_verb_args_are_rejected_not_raised():
    """A malformed element is dropped at parse time; the valid one next to it
    still dispatches and the turn ends cleanly."""
    bus, tools = RecordingBus(), FakeTools()
    envelope = ('{"intent":"control","say":"ok","actions":['
                '{"verb":"seek","to_seconds":1,"delta_seconds":2},'
                '{"verb":"reject_title","title_id":"7"},'
                '{"verb":"focus","title_id":"27205"}]}')
    agent = _agent(FakeOpenAI([envelope]), bus, tools=tools)
    sink = TimingSink()
    await _run(agent, sink, [LLMContextFrame(context=_ctx("seek"))])
    assert (await bus.next_outbound()).verb == "focus"
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(bus.next_outbound(), timeout=0.05)   # no seek reached the bus
    assert tools.calls == [("reject_title", {"title_id": "7"})]     # typed model, not a dict
    assert any(isinstance(f, LLMFullResponseEndFrame) for f in sink.frames)


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
    from tv_avatar.agent.prompt import greeting_instruction
    lane = FakeMemoryLane()
    agent = _agent(FakeOpenAI(['{"intent":"chitchat","say":"Hi!","actions":[]}']), RecordingBus(), lane=lane)
    greeting = greeting_instruction(agent._session.persona.language)
    await _run(agent, TimingSink(), [LLMContextFrame(context=_ctx(greeting))])
    await asyncio.sleep(0.02)
    assert lane.ingests == []


async def test_greeting_in_new_session_sees_last_sessions_history_and_memory(tmp_path):
    """A fresh session for a returning user_id opens with what we talked about last time in the prompt."""
    import time as _t

    from tv_avatar.agent.prompt import greeting_instruction
    from tv_avatar.history.store import Event, EventKind, HistoryStore
    from tv_avatar.recs.catalog import CatalogItem

    class Cat:
        def lookup(self, title_id):
            return CatalogItem(title_id="155", name="The Dark Knight", year=2008) if title_id == "155" else None

    history = HistoryStore(str(tmp_path / "h.db"))
    await history.record(Event(user_id="u1", kind=EventKind.REC_SHOWN, title_id="155", ts=_t.time() - 86400 - 60))
    lane = FakeMemoryLane({"u1": MemoryBlock.from_lines(["loves Batman films"], [])})
    client = FakeOpenAI(['{"intent":"chitchat","say":"Welcome back, want to carry on with The Dark Knight?","actions":[]}'])
    session = SessionState("sess_new", "tok", 0, user_id="u1")  # new session, same user
    agent = SGRAgentService(_settings(), RecordingBus(), lane, None, history, session,
                            catalog=Cat(), client=client, tools=FakeTools())
    greeting = greeting_instruction(session.persona.language)
    await _run(agent, TimingSink(), [LLMContextFrame(context=_ctx(greeting))])
    await history.close()

    system, user = client.calls[0]["messages"][0]["content"], client.calls[0]["messages"][-1]["content"]
    assert "Recently recommended: The Dark Knight (2008) (id=155, yesterday)" in system
    # The greeting brief repeats the context right next to the instruction.
    assert user.startswith(greeting) and "Greet them in English" in user
    assert "Recently recommended: The Dark Knight (2008) (id=155, yesterday)" in user
    # The rules let the agent act on that id next turn ("yes, play it").
    assert "Recent activity" in system.split("Only reference title_ids")[1].split("\n")[0]
    assert "loves Batman films" in user
    # History decides the title, the profile only the tone — and it says so, in that order.
    assert user.index("Recent activity (newest first)") < user.index("Viewer profile (tone only)")
    assert "never take the title from it" in user and "Memory never picks the title" in system
    assert lane.ingests == []  # the synthetic greeting still is not stored as a user utterance


async def test_slow_second_cycle_falls_back_to_templated_answer():
    """Cycle 1 is fast; cycle 2 never yields a say byte in time → template + focus, no 3rd call."""
    class TwoSpeeds(FakeOpenAI):
        async def _create(self, **kwargs):
            self.calls.append(kwargs)
            text = self.scripts.pop(0)
            return FakeStream(text, delay_s=0.0 if len(self.calls) == 1 else 0.3)

    settings = _settings().model_copy(update={"cycle_first_byte_s": 0.15})
    session = SessionState("sess_t", "tok", 0, user_id="u1")
    bus, sink, tools, lane = RecordingBus(), TimingSink(), FakeTools(), FakeMemoryLane()
    client = TwoSpeeds([RECO_1, RECO_2])
    agent = SGRAgentService(settings, bus, lane, None, None, session, client=client, tools=tools)
    await _run(agent, sink, [LLMContextFrame(context=_ctx("recommend me a heist movie"))])

    said = _spoken(sink)
    assert said[0] == "Let me look."
    assert "How about Heat, or Inception?" in said            # from FakeTools' two titles
    assert (await bus.next_outbound()).verb == "focus"        # first title focused
    assert len(client.calls) == 2                              # cycle 2 was attempted, then cancelled
    assert sum(isinstance(f, LLMFullResponseEndFrame) for f in sink.frames) == 1
    await asyncio.sleep(0.02)
    # The template is spoken but is not the agent's reply: memory must not learn
    # "wants Heat" from a substitute the popular channel happened to return.
    assert lane.ingests == [("u1", "recommend me a heist movie", "Let me look.")]


def test_render_fallback_shapes():
    from tv_avatar.agent.service import render_fallback
    from tv_avatar.agent.turn import ToolResult
    text, actions = render_fallback((ToolResult("recommend_titles", {"titles": [
        {"title_id": "1", "name": "Heat", "year": 1995}, {"title_id": "2", "name": "Sicario", "year": 2015},
        {"title_id": "3", "name": "Drive", "year": 2011}, {"title_id": "4", "name": "Extra"}]}),))
    assert text == "How about Heat from 1995, Sicario from 2015, or Drive from 2011?"
    assert actions == [("focus", {"title_id": "1"})]
    assert render_fallback((ToolResult("recommend_titles", {"titles": []}),))[1] == []
    assert "don't have that" in render_fallback((ToolResult("recall_memory", {"memory": "(none yet)"}),))[0]
    assert "took too long" in render_fallback((ToolResult("search_catalog", {"status": "unavailable"}),))[0]


async def test_turn_log_line_reports_typed_metrics():
    """The one INFO line per turn keeps its field set — dashboards grep it."""
    from loguru import logger
    records = []
    handle = logger.add(lambda m: records.append(m.record), level="INFO",
                        filter=lambda r: r["message"] == "turn")
    try:
        client = FakeOpenAI([RECO_1, RECO_2])
        await _run(_agent(client, RecordingBus()), TimingSink(), [LLMContextFrame(context=_ctx("recommend"))])
    finally:
        logger.remove(handle)
    extra = records[-1]["extra"]
    assert extra["cycles"] == 2 and extra["n_actions"] == 2 and extra["intent"] == "recommend"
    assert {"recall_ms", "ttft_ms", "first_action_ms", "total_ms"} <= set(extra)
    assert "fallback" not in extra and extra["turn_id"].startswith("turn_")


async def test_history_is_trimmed_to_recent_messages():
    client = FakeOpenAI([PLAY])
    agent = _agent(client, RecordingBus())
    ctx = LLMContext()
    for i in range(30):
        ctx.add_message({"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"})
    ctx.add_message({"role": "user", "content": "play the first one"})
    await _run(agent, TimingSink(), [LLMContextFrame(context=ctx)])
    sent = client.calls[0]["messages"]
    assert sent[0]["role"] == "system" and len(sent) == 11   # system + last 10
    assert sent[-1]["content"] == "play the first one"
