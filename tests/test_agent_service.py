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


SPACE_SEARCH_1 = '{"intent":"search","say":"Let me look.","actions":[{"verb":"search_catalog","query":"space"}]}'
SPACE_SEARCH_2 = ('{"intent":"search","say":"I found Gravity and Moon.",'
                  '"actions":[{"verb":"show_titles","title_ids":["49047","17431"],"label":"Search results"}]}')
TV_HITS = {"titles": [{"title_id": "49047", "name": "Gravity"}, {"title_id": "17431", "name": "Moon"}]}


class AnsweringBus(RecordingBus):
    """A TV that answers search_catalog straight away, like the frontend does."""

    def __init__(self) -> None:
        super().__init__()
        self.dispatched: list[str] = []

    async def dispatch(self, verb, args, turn_id):
        self.dispatched.append(verb)
        if verb == "search_catalog":
            async def answer():
                msg = await self.next_outbound()
                self.resolve(msg.id, TV_HITS)
            asyncio.get_running_loop().create_task(answer())
        return await super().dispatch(verb, args, turn_id)


async def test_search_catalog_result_is_fed_back_for_a_second_cycle():
    """The TV answers within the budget; its titles must reach the model and be spoken,
    not discarded after the filler (the field bug: 'Let me look.' and silence)."""
    bus, sink = AnsweringBus(), TimingSink()
    client = FakeOpenAI([SPACE_SEARCH_1, SPACE_SEARCH_2])
    agent = _agent(client, bus)
    await _run(agent, sink, [LLMContextFrame(context=_ctx("search for space"))])

    assert len(client.calls) == 2
    assert "Gravity" in client.calls[1]["messages"][-1]["content"]
    assert _spoken(sink) == ["Let me look.", "I found Gravity and Moon."]
    command = await bus.next_outbound()
    assert command.verb == "show_titles"
    assert command.args["title_ids"] == ["49047", "17431"]


async def test_second_cycle_search_is_skipped_not_awaited():
    """Cycle 2 has no cycle 3 to speak a result, so a search there must not block the turn."""
    again = '{"intent":"search","say":"Let me check once more.","actions":[{"verb":"search_catalog","query":"moon"}]}'
    bus, sink = AnsweringBus(), TimingSink()
    agent = _agent(FakeOpenAI([SPACE_SEARCH_1, again]), bus)
    await _run(agent, sink, [LLMContextFrame(context=_ctx("search for space"))])

    assert _spoken(sink) == ["Let me look.", "Let me check once more."]
    assert bus.dispatched == ["search_catalog"]


def test_search_fallback_names_the_hits():
    from tv_avatar.agent.fallback import render_fallback
    from tv_avatar.agent.turn import ToolResult
    text, actions = render_fallback((ToolResult("search_catalog", TV_HITS),))
    assert text == "I found Gravity, or Moon."
    assert actions == [("show_titles", {"title_ids": ["49047", "17431"], "label": "Search results"})]
    assert "couldn't find" in render_fallback((ToolResult("search_catalog", {"titles": []}),))[0]


def test_search_fallback_respects_the_rail_size_limit():
    from tv_avatar.agent.envelope import parse_action
    from tv_avatar.agent.fallback import render_fallback
    from tv_avatar.agent.turn import ToolResult

    titles = [{"title_id": str(i), "name": f"Movie {i}"} for i in range(50)]
    _, actions = render_fallback((ToolResult("search_catalog", {"titles": titles}),))
    verb, args = actions[0]
    action = parse_action({"verb": verb, **args}, final=True)
    assert action.title_ids == [str(i) for i in range(20)]


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


async def test_two_cycle_turn_is_ingested_once_with_a_space_between_cycles():
    lane = FakeMemoryLane()
    agent = _agent(FakeOpenAI([RECO_1, RECO_2]), RecordingBus(), lane=lane)
    await _run(agent, TimingSink(), [LLMContextFrame(context=_ctx("recommend me a heist movie"))])
    await asyncio.sleep(0.02)
    assert lane.ingests == [("u1", "recommend me a heist movie", "Let me look. Try Heat or Inception.")]


@pytest.mark.parametrize("max_cycles", [1, 2, 3])
async def test_cycles_are_capped_at_setting(max_cycles):
    """The model asks for a tool on every cycle; the loop still ends at AGENT_MAX_CYCLES.
    A real decoder cannot emit the tool on the final cycle (narrowed schema); this
    canned envelope still does, and is rejected rather than run without a reply."""
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


async def test_final_cycle_is_decoded_against_the_narrowed_schema():
    """The cap is the schema, not a prompt hint: only the last allowed cycle is
    sent `turn_plan_final`, which cannot express an observation tool."""
    settings = _settings().model_copy(update={"agent_max_cycles": 3})
    client = FakeOpenAI([RECO_1, RECO_1, RECO_2])
    agent = SGRAgentService(settings, RecordingBus(), FakeMemoryLane(), None, None,
                            SessionState("sess_t", "tok", 0, user_id="u1"), client=client, tools=FakeTools())
    await _run(agent, TimingSink(), [LLMContextFrame(context=_ctx("recommend"))])
    assert len(client.calls) == 3
    assert [c["response_format"]["json_schema"]["name"] for c in client.calls] == [
        "turn_plan", "turn_plan", "turn_plan_final"]
    second, third = client.calls[1]["messages"][-1]["content"], client.calls[2]["messages"][-1]["content"]
    assert second.startswith("[tool results]") and third.startswith("[tool results]")
    assert "Heat" in second and "Heat" in third
    # Every follow-up cycle carries the whole exchange so far: envelope, results, envelope, results.
    assert [m["role"] for m in client.calls[2]["messages"][-4:]] == ["assistant", "user", "assistant", "user"]


async def test_turn_ends_when_a_cycle_yields_no_observation():
    """The model, not the cap, ends the turn: with room for four cycles it stops
    after the answer cycle because that one asked for nothing."""
    settings = _settings().model_copy(update={"agent_max_cycles": 4})
    client = FakeOpenAI([RECO_1, RECO_2, RECO_1, RECO_1])
    agent = SGRAgentService(settings, RecordingBus(), FakeMemoryLane(), None, None,
                            SessionState("sess_t", "tok", 0, user_id="u1"), client=client, tools=FakeTools())
    sink = TimingSink()
    await _run(agent, sink, [LLMContextFrame(context=_ctx("recommend"))])
    assert len(client.calls) == 2
    assert _spoken(sink) == ["Let me look.", "Try Heat or Inception."]


async def test_first_cycle_is_never_budgeted():
    """Only follow-up cycles race the first-byte budget; a slow first cycle
    still speaks — there is nothing to fall back to yet."""
    settings = _settings().model_copy(update={"cycle_first_byte_s": 0.05})
    client, sink, bus = FakeOpenAI([PLAY], delay_s=0.03), TimingSink(), RecordingBus()
    agent = SGRAgentService(settings, bus, FakeMemoryLane(), None, None,
                            SessionState("sess_t", "tok", 0, user_id="u1"), client=client, tools=FakeTools())
    await _run(agent, sink, [LLMContextFrame(context=_ctx("play it"))])
    assert _spoken(sink) == ["On it."]
    assert (await bus.next_outbound()).verb == "play"


async def test_follow_up_waits_past_the_old_deadline_by_default():
    class SlowFollowUp(FakeOpenAI):
        async def _create(self, **kwargs):
            if self.calls:
                await asyncio.sleep(1.3)
            return await super()._create(**kwargs)

    bus, sink = RecordingBus(), TimingSink()
    client = SlowFollowUp([RECO_1, RECO_2])
    await _run(_agent(client, bus), sink, [LLMContextFrame(context=_ctx("recommend"))])
    assert len(client.calls) == 2
    assert _spoken(sink) == ["Let me look.", "Try Heat or Inception."]
    assert (await bus.next_outbound()).verb == "focus"


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
    assert (await bus.next_outbound()).verb == "show_titles"


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


@pytest.mark.parametrize("verb", ["play", "search_catalog"])
async def test_interruption_cancels_actions_started_while_the_envelope_is_streaming(verb):
    envelope = PLAY if verb == "play" else SPACE_SEARCH_1

    class DelayedBus(RecordingBus):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.cancelled = False

        async def dispatch(self, verb, args, turn_id):
            self.started.set()
            try:
                await self.release.wait()
                return await super().dispatch(verb, args, turn_id)
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    class PausedStream(FakeStream):
        async def _iter(self):
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=envelope[:-2]))])
            await asyncio.sleep(10)

    class PausedClient(FakeOpenAI):
        async def _create(self, **kwargs):
            self.calls.append(kwargs)
            return PausedStream(envelope)

    bus = DelayedBus()
    agent = _agent(PausedClient([]), bus)
    await _run(agent, TimingSink(), [LLMContextFrame(context=_ctx("play")), SleepFrame(0.1), InterruptionFrame()])
    assert bus.started.is_set()
    bus.release.set()
    await asyncio.sleep(0.02)
    assert bus.cancelled
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(bus.next_outbound(), timeout=0.02)


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


@pytest.mark.parametrize("screen_kind", ["unknown", "offscreen", "onscreen"])
async def test_shop_context_reaches_sgr_without_the_injector(monkeypatch, screen_kind):
    from tv_avatar.control.protocol import Playback, ScreenState, Tile
    from tv_avatar.shop import ShopCatalog

    shop = ShopCatalog.model_validate({"346698": [{
        "id": "bomber", "name": "Pink Satin Bomber Jacket", "brand": "Dreamhouse",
        "price": "€89", "image": "products/bomber.jpg",
    }]})
    monkeypatch.setattr("tv_avatar.agent.prompt.get_shop", lambda: shop)
    reply = ('{"intent":"control","say":"The jacket is eighty-nine euros.",'
             '"actions":[{"verb":"show_products","title_id":"346698"}]}')
    client, bus, sink = FakeOpenAI([reply]), RecordingBus(), TimingSink()
    agent = _agent(client, bus)
    agent._cfg.agent_max_cycles = 1
    agent._catalog = SimpleNamespace(lookup=lambda title_id: SimpleNamespace(
        name="Barbie", label=lambda: "Barbie (2023)") if title_id == "346698" else None)
    if screen_kind != "unknown":
        tile = (Tile(title_id="346698", name="Barbie", position=0, shoppable=True)
                if screen_kind == "onscreen"
                else Tile(title_id="565770", name="Blue Beetle", position=0))
        agent._session.update_screen(ScreenState(
            view="grid", focus_index=0, tiles=[tile], playback=Playback(state="stopped")))
    context = _ctx("show me the Barbie jacket")
    context.add_message({"role": "system", "content": "# Shop\nSTALE SHELF"})
    await _run(agent, sink, [LLMContextFrame(context=context)])

    system = client.calls[0]["messages"][0]["content"]
    assert system.count("# Shop\n") == 1
    assert "# Shop\n- Barbie (id=346698): Pink Satin Bomber Jacket €89" in system
    assert "STALE SHELF" not in system
    screen = system.split("# Screen\n")[1].split("\n\n# ")[0]
    if screen_kind == "onscreen":
        assert "Barbie (2023) (id=346698) [shop] <- focused" in screen
    else:
        assert "Barbie" not in screen
    command = await bus.next_outbound()
    assert command.verb == "show_products" and command.args == {"title_id": "346698"}
    assert len(client.calls) == 1
    assert _spoken(sink) == ["The jacket is eighty-nine euros."]


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


async def test_greeting_instruction_leaves_the_history_after_the_greeting_turn():
    """Live: with the stage direction still in the conversation, "hello" and even
    "yes" were answered with the greeting again. It is an instruction to the model,
    not something the viewer said, so later turns must not see it — while the
    greeting turn itself still gets the brief in its place."""
    from tv_avatar.agent.prompt import GREETING_PREFIX, greeting_instruction
    client = FakeOpenAI(['{"intent":"chitchat","say":"Welcome back!","actions":[]}',
                         '{"intent":"chitchat","say":"Hello there.","actions":[]}'])
    agent = _agent(client, RecordingBus())
    ctx = LLMContext()
    ctx.add_message({"role": "user", "content": greeting_instruction(agent._session.persona.language)})
    await _run(agent, TimingSink(), [LLMContextFrame(context=ctx)])
    ctx.add_message({"role": "assistant", "content": "Welcome back!"})
    ctx.add_message({"role": "user", "content": "hello"})
    await _run(agent, TimingSink(), [LLMContextFrame(context=ctx)])

    first, second = client.calls[0]["messages"], client.calls[1]["messages"]
    assert first[-1]["content"].startswith(GREETING_PREFIX)          # the brief, on the greeting turn
    assert [m["role"] for m in second] == ["system", "assistant", "user"]
    assert not any(GREETING_PREFIX in m["content"] for m in second[1:])
    assert second[-1]["content"] == "hello"


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
    assert (await bus.next_outbound()).verb == "show_titles"  # the picks go on screen
    assert len(client.calls) == 2                              # cycle 2 was attempted, then cancelled
    assert sum(isinstance(f, LLMFullResponseEndFrame) for f in sink.frames) == 1
    await asyncio.sleep(0.02)
    # The template is spoken but is not the agent's reply: memory must not learn
    # "wants Heat" from a substitute the popular channel happened to return.
    assert lane.ingests == [("u1", "recommend me a heist movie", "Let me look.")]


class FakeRecorder:
    """HistoryRecorder stand-in: records rec_shown calls with timestamps."""
    def __init__(self) -> None:
        self.shown: list[tuple[float, list[str]]] = []

    async def on_rec_shown(self, user_id, ids):
        self.shown.append((time.perf_counter(), list(ids)))

    def spawn(self, coro):
        return asyncio.create_task(coro)


class FiveTitles(FakeTools):
    async def run(self, action, user_id):
        await super().run(action, user_id)
        return {"titles": [{"title_id": i, "name": n} for i, n in
                           (("949", "Heat"), ("27205", "Inception"), ("680", "Pulp Fiction"),
                            ("8", "Drive"), ("9", "Extra"))]}


def _agent_with_recorder(client, settings=None, **kw):
    recorder = FakeRecorder()
    session = SessionState("sess_t", "tok", 0, user_id="u1")
    agent = SGRAgentService(settings or _settings(), kw.pop("bus", RecordingBus()), kw.pop("lane", FakeMemoryLane()),
                            None, None, session, client=client, tools=kw.pop("tools", FiveTitles()),
                            recorder=recorder)
    return agent, recorder


async def test_rec_shown_records_only_what_was_offered_after_the_turn_ends():
    """Five candidates came back; the agent named two and focused one of them.
    Only those two are logged as shown, and only once the turn has closed."""
    agent, recorder = _agent_with_recorder(FakeOpenAI([RECO_1, RECO_2]))
    end_at: list[float] = []
    original_push = agent.push_frame

    async def timed_push(frame, direction=FrameDirection.DOWNSTREAM):   # push is async to the sink
        if isinstance(frame, LLMFullResponseEndFrame):
            end_at.append(time.perf_counter())
        await original_push(frame, direction)

    agent.push_frame = timed_push
    await _run(agent, TimingSink(), [LLMContextFrame(context=_ctx("recommend me a heist movie"))])
    await asyncio.sleep(0.02)
    assert [ids for _, ids in recorder.shown] == [["949", "27205"]]
    assert recorder.shown[0][0] > end_at[0]


async def test_rec_shown_from_the_templated_fallback():
    class TwoSpeeds(FakeOpenAI):
        async def _create(self, **kwargs):
            self.calls.append(kwargs)
            return FakeStream(self.scripts.pop(0), delay_s=0.0 if len(self.calls) == 1 else 0.3)

    settings = _settings().model_copy(update={"cycle_first_byte_s": 0.15})
    agent, recorder = _agent_with_recorder(TwoSpeeds([RECO_1, RECO_2]), settings)
    sink = TimingSink()
    await _run(agent, sink, [LLMContextFrame(context=_ctx("recommend"))])
    await asyncio.sleep(0.02)
    assert "How about Heat, Inception, or Pulp Fiction?" in _spoken(sink)
    assert [ids for _, ids in recorder.shown] == [["949", "27205", "680", "8", "9"]]   # all five appear on the rail


async def test_interrupted_turn_records_nothing_as_shown():
    slow = FakeOpenAI([RECO_1, RECO_2], delay_s=0.05)
    agent, recorder = _agent_with_recorder(slow)
    await _run(agent, TimingSink(), [LLMContextFrame(context=_ctx("recommend")), SleepFrame(0.3), InterruptionFrame()])
    await asyncio.sleep(0.02)
    assert recorder.shown == []


async def test_a_turn_without_recommendations_records_nothing():
    agent, recorder = _agent_with_recorder(FakeOpenAI([PLAY]))
    await _run(agent, TimingSink(), [LLMContextFrame(context=_ctx("play the first one"))])
    await asyncio.sleep(0.02)
    assert recorder.shown == []


def test_render_fallback_shapes():
    from tv_avatar.agent.fallback import render_fallback
    from tv_avatar.agent.turn import ToolResult
    text, actions = render_fallback((ToolResult("recommend_titles", {"titles": [
        {"title_id": "1", "name": "Heat", "year": 1995}, {"title_id": "2", "name": "Sicario", "year": 2015},
        {"title_id": "3", "name": "Drive", "year": 2011}, {"title_id": "4", "name": "Extra"}]}),))
    assert text == "How about Heat from 1995, Sicario from 2015, or Drive from 2011?"
    # Every returned title goes on the rail, even the ones not spoken.
    assert actions == [("show_titles", {"title_ids": ["1", "2", "3", "4"], "label": "For you"})]
    assert render_fallback((ToolResult("recommend_titles", {"titles": []}),))[1] == []
    assert "didn't respond" in render_fallback((ToolResult("search_catalog", {"status": "unavailable"}),))[0]
    assert "couldn't find" in render_fallback((ToolResult("search_catalog", {"titles": []}),))[0]


SEARCH_1 = ('{"intent":"search","say":"Searching for Jurassic World.",'
            '"actions":[{"verb":"search_catalog","query":"Jurassic World"}]}')
SEARCH_2 = '{"intent":"answer","say":"The search did not go through. Try again?","actions":[]}'


async def test_search_nobody_answers_is_spoken_not_swallowed():
    """Seen live: with no TV app on the control socket, `search_catalog` timed
    out and the turn ended on "Searching for X." — the viewer waited on nothing."""
    client, sink = FakeOpenAI([SEARCH_1, SEARCH_2]), TimingSink()
    await _run(_agent(client, CommandBus(search_timeout_s=0.02)), sink, [LLMContextFrame(context=_ctx("jurassic world"))])
    assert _spoken(sink) == ["Searching for Jurassic World.", "The search did not go through.", "Try again?"]
    assert len(client.calls) == 2
    feedback = client.calls[1]["messages"][-1]["content"]
    assert "[tool results]" in feedback and '"status": "unavailable"' in feedback


async def test_search_in_the_only_cycle_is_rejected_by_the_schema():
    """With no follow-up cycle available, search must not dispatch or await the TV."""
    bus, client, sink = AnsweringBus(), FakeOpenAI([SEARCH_1]), TimingSink()
    agent = _agent(client, bus)
    agent._cfg.agent_max_cycles = 1
    await _run(agent, sink, [LLMContextFrame(context=_ctx("jurassic world"))])
    assert _spoken(sink) == ["Searching for Jurassic World."]
    assert len(client.calls) == 1
    assert bus.dispatched == []
    assert "SearchCatalog" not in client.calls[0]["response_format"]["json_schema"]["schema"]["$defs"]


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


# --- spans (observability plan, Task 3) -----------------------------------------
# `_tracing_enabled` engages Pipecat's @traced_llm; AIService.setup() resets it from
# the StartFrame, so it is re-applied after setup. With no turn context the `llm`
# span is a root, which is fine for asserting the subtree.


def _traced(agent):
    original_setup = agent.setup

    async def setup(cfg):
        await original_setup(cfg)
        agent._tracing_enabled = True

    agent.setup = setup
    return agent


async def test_turn_produces_llm_recall_cycle_action_spans(otel):
    agent = _agent(FakeOpenAI([PLAY]), RecordingBus(), tools=FakeTools())
    _traced(agent)
    await _run(agent, TimingSink(), [LLMContextFrame(context=_ctx("play the first one"))])
    spans = otel.spans()
    llm, = spans["llm"]
    assert llm.attributes["langfuse.observation.metadata.intent"] == "control"
    assert llm.attributes["langfuse.observation.metadata.cycles"] == 1
    assert llm.attributes["langfuse.observation.metadata.greeting"] is False
    assert llm.attributes["langfuse.observation.input"] == "play the first one"
    assert llm.attributes["langfuse.trace.input"] == "play the first one"
    assert llm.attributes["langfuse.observation.output"] == "On it." == llm.attributes["langfuse.trace.output"]
    assert llm.attributes["tv.turn_id"].startswith("turn_") and "tv.turn.total_ms" in llm.attributes
    assert llm.attributes["gen_ai.request.model"] == _settings().llm_model   # Pipecat's own attribute
    recall, = spans["agent.recall"]
    assert recall.parent.span_id == llm.context.span_id
    assert recall.attributes["langfuse.observation.type"] == "retriever"
    cycle, = spans["agent.cycle"]
    assert cycle.parent.span_id == llm.context.span_id and cycle.attributes["tv.cycle.max"] == 2
    assert cycle.attributes["langfuse.observation.type"] == "generation"
    assert cycle.attributes["gen_ai.request.model"] == _settings().llm_model
    assert cycle.attributes["langfuse.observation.output"] == PLAY
    assert cycle.attributes["tv.cycle.n_actions"] == 1 and "tv.cycle.ttft_ms" in cycle.attributes
    assert '"role": "system"' in cycle.attributes["langfuse.observation.input"]
    action, = spans["agent.action"]
    assert action.attributes["langfuse.observation.type"] == "tool"
    assert action.attributes["langfuse.observation.metadata.verb"] == "play"
    assert action.attributes["langfuse.observation.metadata.kind"] == "tv"
    assert action.attributes["langfuse.observation.metadata.status"] == "dispatched"
    assert action.attributes["tv.action.returns_observation"] is False
    assert action.parent.span_id == cycle.context.span_id


async def test_two_cycle_turn_records_both_cycles_and_offered_ids(otel):
    agent = _agent(FakeOpenAI([RECO_1, RECO_2]), RecordingBus(), tools=FakeTools())
    _traced(agent)
    await _run(agent, TimingSink(), [LLMContextFrame(context=_ctx("recommend me a heist movie"))])
    spans = otel.spans()
    cycles = sorted(spans["agent.cycle"], key=lambda s: s.attributes["langfuse.observation.metadata.cycle"])
    assert [c.attributes["langfuse.observation.metadata.cycle"] for c in cycles] == [1, 2]
    recommend = next(a for a in spans["agent.action"]
                     if a.attributes["langfuse.observation.metadata.verb"] == "recommend_titles")
    assert recommend.attributes["langfuse.observation.metadata.kind"] == "internal"
    assert recommend.attributes["tv.action.awaits_result"] is True
    assert '"Heat"' in recommend.attributes["langfuse.observation.output"]
    llm, = spans["llm"]
    assert llm.attributes["langfuse.observation.metadata.cycles"] == 2
    assert set(llm.attributes["tv.turn.offered_ids"]) == {"949", "27205"}   # Heat, Inception — both named
    assert llm.attributes["langfuse.observation.output"] == "Let me look. Try Heat or Inception."


async def test_slow_follow_up_cycle_records_over_budget_and_fallback(otel):
    class TwoSpeeds(FakeOpenAI):
        async def _create(self, **kwargs):
            self.calls.append(kwargs)
            return FakeStream(self.scripts.pop(0), delay_s=0.0 if len(self.calls) == 1 else 0.3)

    settings = _settings().model_copy(update={"cycle_first_byte_s": 0.15})
    agent = SGRAgentService(settings, RecordingBus(), FakeMemoryLane(), None, None,
                            SessionState("sess_t", "tok", 0, user_id="u1"),
                            client=TwoSpeeds([RECO_1, RECO_2]), tools=FakeTools())
    _traced(agent)
    await _run(agent, TimingSink(), [LLMContextFrame(context=_ctx("recommend me a heist movie"))])
    spans = otel.spans()
    cycle2 = next(c for c in spans["agent.cycle"] if c.attributes["langfuse.observation.metadata.cycle"] == 2)
    assert cycle2.attributes["langfuse.observation.metadata.over_budget"] is True
    assert cycle2.attributes["tv.cycle.budget_ms"] == 150
    fallback, = spans["agent.fallback"]
    assert fallback.attributes["langfuse.observation.metadata.cycle"] == 2
    assert fallback.attributes["langfuse.observation.level"] == "WARNING"
    assert fallback.attributes["langfuse.observation.output"].startswith("How about Heat")
    assert fallback.attributes["tv.fallback.n_actions"] == 1
    assert spans["llm"][0].attributes["langfuse.observation.metadata.fallback"] is True
    assert cycle2.attributes["langfuse.observation.metadata.stop_reason"] == "budget_exceeded"
    submitted = [s for s in spans["agent.speech"]
                 if s.attributes["langfuse.observation.metadata.source"] == "fallback"]
    assert len(submitted) == 1
    assert submitted[0].attributes["langfuse.observation.output"].startswith("How about Heat")


async def test_cycle_cap_refusal_and_parse_rejection_are_counted(otel):
    """agent_max_cycles=1: the only cycle is the final one, so recommend_titles is
    not in its union and is rejected exactly like the malformed seek; reject_title runs."""
    envelope = ('{"intent":"control","say":"ok","actions":['
                '{"verb":"seek","to_seconds":1,"delta_seconds":2},'
                '{"verb":"reject_title","title_id":"7"},'
                '{"verb":"recommend_titles","query":"heist"}]}')
    settings = _settings().model_copy(update={"agent_max_cycles": 1})
    client = FakeOpenAI([envelope])
    agent = SGRAgentService(settings, RecordingBus(), FakeMemoryLane(), None, None,
                            SessionState("sess_t", "tok", 0, user_id="u1"), client=client, tools=FakeTools())
    _traced(agent)
    await _run(agent, TimingSink(), [LLMContextFrame(context=_ctx("seek"))])
    spans = otel.spans()
    cycle, = spans["agent.cycle"]
    assert client.calls[0]["response_format"]["json_schema"]["name"] == "turn_plan_final"
    assert cycle.attributes["tv.cycle.n_rejected"] == 2 and "tv.cycle.n_skipped" not in cycle.attributes
    assert cycle.attributes["tv.cycle.n_actions"] == 1
    assert [e.name for e in cycle.events] == ["tv.action.rejected", "tv.action.rejected"]
    assert [e.attributes["verb"] for e in cycle.events] == ["seek", "recommend_titles"]
    assert [a.attributes["langfuse.observation.metadata.verb"] for a in spans["agent.action"]] == ["reject_title"]


async def test_interruption_records_partial_turn(otel):
    slow = FakeOpenAI(['{"intent":"chitchat","say":"I love that you love sci-fi, let me think about it some more.","actions":[]}'],
                      delay_s=0.05)
    agent = _agent(slow, RecordingBus())
    _traced(agent)
    await _run(agent, TimingSink(), [LLMContextFrame(context=_ctx("I love sci-fi")), SleepFrame(0.4), InterruptionFrame()])
    spans = otel.spans()
    llm, = spans["llm"]
    assert llm.attributes["langfuse.observation.metadata.interrupted"] is True
    assert llm.attributes["tv.turn.dropped_commands"] == 0
    assert 0 < len(llm.attributes["langfuse.observation.output"]) < len("I love that you love sci-fi, let me think about it some more.")
    cycle, = spans["agent.cycle"]
    assert cycle.attributes["langfuse.observation.output"].startswith('{"intent":"chitchat"')   # partial envelope kept
    assert cycle.attributes["langfuse.observation.metadata.stop_reason"] == "interrupted"
    assert cycle.attributes["langfuse.observation.metadata.validation"] == "incomplete"


async def test_cycle_records_validated_plan_and_speech_submission(otel):
    import json

    agent = _traced(_agent(FakeOpenAI([PLAY]), RecordingBus()))
    await _run(agent, TimingSink(), [LLMContextFrame(context=_ctx("play"))])
    spans = otel.spans()
    cycle, = spans["agent.cycle"]
    meta = "langfuse.observation.metadata."
    assert cycle.attributes[meta + "stop_reason"] == "envelope_complete"
    assert cycle.attributes[meta + "schema_name"] == "turn_plan"
    assert cycle.attributes[meta + "usage_available"] is False
    assert cycle.attributes[meta + "validation"] == "valid"
    plan, = spans["agent.plan"]
    assert json.loads(plan.attributes["langfuse.observation.output"])["plan"]["say"] == "On it."
    speech, = spans["agent.speech"]
    assert speech.attributes[meta + "status"] == "submitted"
    assert speech.attributes[meta + "source"] == "model"
    assert speech.attributes["langfuse.observation.output"] == "On it."
    assert spans["llm"][0].attributes["langfuse.observation.type"] == "agent"
    assert spans["agent.action"][0].attributes[meta + "action_index"] == 0


async def test_incomplete_envelope_is_diagnostic_not_a_successful_plan(otel):
    agent = _traced(_agent(FakeOpenAI(['{"intent":"answer","say":"partial']), RecordingBus()))
    await _run(agent, TimingSink(), [LLMContextFrame(context=_ctx("hello"))])
    cycle, = otel.spans()["agent.cycle"]
    assert cycle.attributes["langfuse.observation.metadata.validation"] == "incomplete"
    assert cycle.attributes["langfuse.observation.metadata.stop_reason"] == "provider_eof"


async def test_finish_reason_and_usage_are_not_confused_with_application_stop(otel):
    class WithUsage(FakeOpenAI):
        async def _create(self, **kwargs):
            async def chunks():
                yield SimpleNamespace(model="reported-model", id="completion-1",
                    usage=SimpleNamespace(prompt_tokens=12, completion_tokens=8),
                    choices=[SimpleNamespace(delta=SimpleNamespace(content=PLAY), finish_reason="stop")])
            return chunks()

    agent = _traced(_agent(WithUsage([]), RecordingBus()))
    await _run(agent, TimingSink(), [LLMContextFrame(context=_ctx("play"))])
    cycle, = otel.spans()["agent.cycle"]
    assert cycle.attributes["langfuse.observation.metadata.finish_reason"] == "stop"
    assert cycle.attributes["langfuse.observation.metadata.stop_reason"] == "envelope_complete"
    assert cycle.attributes["gen_ai.usage.input_tokens"] == 12
    assert cycle.attributes["langfuse.observation.metadata.usage_available"] is True
    assert cycle.attributes["gen_ai.response.model"] == "reported-model"


async def test_provider_failure_retains_partial_json_without_claiming_completion(otel):
    class Broken(FakeOpenAI):
        async def _create(self, **kwargs):
            async def chunks():
                yield SimpleNamespace(choices=[SimpleNamespace(
                    delta=SimpleNamespace(content='{"intent":"answer","say":"part'), finish_reason=None)])
                raise ConnectionError("private provider response")
            return chunks()

    agent = _traced(_agent(Broken([]), RecordingBus()))
    await _run(agent, TimingSink(), [LLMContextFrame(context=_ctx("hello"))])
    cycle, = otel.spans()["agent.cycle"]
    assert cycle.attributes["langfuse.observation.metadata.stop_reason"] == "provider_error"
    assert cycle.attributes["langfuse.observation.metadata.validation"] == "incomplete"
    assert cycle.status.status_code.name == "ERROR"
    assert cycle.attributes["langfuse.observation.output"].endswith("part")
    assert "agent.speech" not in otel.spans()


async def test_tracing_off_opens_no_spans(otel):
    """The default: no `_tracing_enabled`, no provider branch in business code —
    observation() still runs, but its spans are the no-op kind Pipecat's turn
    would have parented; nothing of ours is recorded."""
    agent = _agent(FakeOpenAI([PLAY]), RecordingBus())
    await _run(agent, TimingSink(), [LLMContextFrame(context=_ctx("play the first one"))])
    assert "llm" not in otel.spans()


async def test_barge_in_after_a_finished_turn_does_not_touch_its_closed_span(otel, caplog):
    """The viewer interrupts playback of a reply whose turn already ended: the
    queued commands are still dropped, but nothing is written to the ended span."""
    import logging
    agent = _agent(FakeOpenAI([PLAY]), RecordingBus())
    _traced(agent)
    await _run(agent, TimingSink(), [LLMContextFrame(context=_ctx("play the first one")), SleepFrame(0.05),
                                     InterruptionFrame()])
    llm, = otel.spans()["llm"]
    assert "langfuse.observation.metadata.interrupted" not in llm.attributes
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING and "ended span" in r.getMessage()]
