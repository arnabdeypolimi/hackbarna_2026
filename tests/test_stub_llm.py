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
