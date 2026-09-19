from tv_avatar.agent.llm import DEFAULT_SCRIPT, ScriptedTurn, StubLLMService
from tv_avatar.control.bus import CommandBus


def test_default_script_is_non_empty_and_well_formed():
    assert DEFAULT_SCRIPT
    for turn in DEFAULT_SCRIPT:
        assert turn.text
        for verb, args in turn.commands:
            assert isinstance(verb, str)
            assert isinstance(args, dict)


def test_script_advances_and_wraps():
    script = [
        ScriptedTurn("first", []),
        ScriptedTurn("second", [("pause", {})]),
    ]
    svc = StubLLMService(bus=CommandBus(), script=script)
    assert svc.next_turn().text == "first"
    assert svc.next_turn().text == "second"
    assert svc.next_turn().text == "first"


async def test_running_a_turn_dispatches_its_commands():
    bus = CommandBus()
    svc = StubLLMService(
        bus=bus, script=[ScriptedTurn("ok", [("play", {"title_id": "tt_3"})])]
    )
    text = await svc.run_scripted_turn(turn_id="turn_1")
    assert text == "ok"
    msg = await bus.next_outbound()
    assert msg.verb == "play"
    assert msg.args["title_id"] == "tt_3"


# --- pipeline behaviour: the stub must act as a real LLM stage -------------

import asyncio

import pytest
from pipecat.frames.frames import (
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.tests.utils import run_test

from conftest import PERSONA
from tv_avatar.session.state import SessionStore


def _llm_frames(frames):
    return [
        type(f).__name__
        for f in frames
        if isinstance(f, (LLMFullResponseStartFrame, LLMTextFrame, LLMFullResponseEndFrame))
    ]


async def test_context_frame_produces_start_text_end_and_dispatches_commands():
    bus = CommandBus()
    svc = StubLLMService(
        bus=bus, script=[ScriptedTurn("hello there", [("home", {})])]
    )
    down, _ = await run_test(svc, frames_to_send=[LLMContextFrame(LLMContext())])

    assert _llm_frames(down) == [
        "LLMFullResponseStartFrame", "LLMTextFrame", "LLMFullResponseEndFrame"
    ]
    assert [f.text for f in down if isinstance(f, LLMTextFrame)] == ["hello there"]
    msg = await asyncio.wait_for(bus.next_outbound(), timeout=0.1)
    assert msg.verb == "home"


async def test_turn_id_comes_from_the_session_when_one_is_attached():
    bus = CommandBus()
    session = SessionStore().create(60, PERSONA)
    svc = StubLLMService(
        bus=bus, session=session, script=[ScriptedTurn("ok", [("pause", {})])]
    )
    await run_test(svc, frames_to_send=[LLMContextFrame(LLMContext())])

    msg = await asyncio.wait_for(bus.next_outbound(), timeout=0.1)
    assert session.current_turn_id is not None
    assert msg.turn_id == session.current_turn_id


async def test_interruption_cancels_the_current_turns_unsent_commands():
    # InterruptionFrame is a Pipecat system frame: it bypasses the input queue
    # and would reach the stub *before* a queued LLMContextFrame runs. So the
    # turn is run through the pipeline first, then barge-in is applied.
    bus = CommandBus()
    svc = StubLLMService(
        bus=bus, script=[ScriptedTurn("two things", [("pause", {}), ("home", {})])]
    )
    await run_test(svc, frames_to_send=[LLMContextFrame(LLMContext())])

    assert svc.cancel_current_turn() == 2
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(bus.next_outbound(), timeout=0.05)
    assert svc.cancel_current_turn() == 0  # idempotent


async def test_interruption_frame_is_forwarded_downstream():
    bus = CommandBus()
    svc = StubLLMService(bus=bus)
    down, _ = await run_test(svc, frames_to_send=[InterruptionFrame()])
    assert any(isinstance(f, InterruptionFrame) for f in down)
