"""The developer tools under tools/ fail loudly and early when unconfigured."""
import asyncio
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_langfuse_smoke_exits_nonzero_when_unconfigured(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LANGFUSE", "TRACING", "OTEL"))}
    env.update({"TRACING_ENABLED": "false", "NEBIUS_API_KEY": "n", "SLNG_API_KEY": "s", "ANAM_API_KEY": "a"})
    proc = subprocess.run([sys.executable, str(ROOT / "tools" / "langfuse_smoke.py")],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60, check=False)  # no .env in cwd
    assert proc.returncode == 2
    assert "TRACING_ENABLED=true" in proc.stderr


async def test_smoke_tv_answers_search_while_the_agent_is_running():
    from conftest import PERSONA

    from tools.smoke_turn import SimulatedTV
    from tv_avatar.control.bus import CommandBus
    from tv_avatar.control.protocol import Playback, ScreenState, Tile
    from tv_avatar.session.state import SessionState

    bus = CommandBus()
    session = SessionState("test", "token", 0, persona=PERSONA)
    session.update_screen(ScreenState(view="grid", tiles=[], playback=Playback(state="stopped")))
    titles = [Tile(title_id="17431", name="Moon", position=0)]
    async with SimulatedTV(bus, session, titles) as tv:
        result = await asyncio.wait_for(bus.dispatch("search_catalog", {"query": "Moon"}, "t1"), 0.5)
        assert result == {"titles": [{"title_id": "17431", "name": "Moon"}]}
        await bus.dispatch("play", {"title_id": "17431"}, "t1")
    assert [c.verb for c in tv.commands] == ["search_catalog", "play"]
    assert session.screen.playback.title_id == "17431"
    assert tv.task.done()


async def test_routing_evaluation_checks_actions_without_opening_real_runtime(monkeypatch):
    from test_agent_service import FakeOpenAI, _settings

    from tools.routing_eval import FIXTURES, RequestBudget, RoutingSuite, evaluate

    def forbidden(*args, **kwargs):
        raise AssertionError("evaluation must not open real runtime")

    monkeypatch.setattr("tools.smoke_turn.build_runtime", forbidden)
    suite = RoutingSuite.model_validate_json(FIXTURES.read_text())
    case = next(c for c in suite.cases if c.id == "watch_lookup")
    budget = RequestBudget(2)
    client = FakeOpenAI([
        '{"intent":"control","request":{"operation":"play","title":"Moon","title_id":null},"say":"Let me look.","actions":[{"verb":"search_catalog","query":"Moon"}]}',
        '{"intent":"control","request":{"operation":"play","title":"Moon","title_id":"17431"},"say":"On it.","actions":[{"verb":"play","title_id":"17431"}]}',
    ])
    result = await evaluate(case, suite, _settings(), client, budget)
    assert result["passed"], result["failures"]
    assert budget.used == 2
    assert [c["verb"] for c in result["commands"]] == ["search_catalog", "play"]


async def test_routing_evaluation_fails_when_the_budget_stops_the_answer():
    from test_agent_service import FakeOpenAI, _settings

    from tools.routing_eval import FIXTURES, RequestBudget, RoutingSuite, evaluate

    suite = RoutingSuite.model_validate_json(FIXTURES.read_text())
    case = next(c for c in suite.cases if c.id == "missing")
    client = FakeOpenAI([
        '{"intent":"search","request":{"operation":"play","title":"The Glass Lighthouse","title_id":null},"say":"Let me look.","actions":[{"verb":"search_catalog","query":"The Glass Lighthouse"}]}',
    ])
    result = await evaluate(case, suite, _settings(), client, RequestBudget(1))
    assert not result["passed"]
    assert "turn did not complete" in result["failures"]
    assert len(client.calls) == 1


async def test_routing_evaluation_fails_a_well_formed_but_wrong_action():
    from test_agent_service import FakeOpenAI, _settings

    from tools.routing_eval import FIXTURES, RequestBudget, RoutingSuite, evaluate

    suite = RoutingSuite.model_validate_json(FIXTURES.read_text())
    case = next(c for c in suite.cases if c.id == "watch_shop_id")
    client = FakeOpenAI([
        '{"intent":"control","request":{"operation":"shop","title":"Barbie","title_id":"346698"},"say":"Here it is.","actions":[{"verb":"show_products","title_id":"346698"}]}',
    ])
    result = await evaluate(case, suite, _settings(), client, RequestBudget(1))
    assert not result["passed"]
    assert any("unexpected action sequence" in f for f in result["failures"])


def test_routing_score_checks_the_decoded_request_not_only_the_actions():
    """A turn that reaches the right screen for the wrong reason still fails:
    the operation is the routing decision under test."""
    import json

    from tools.routing_eval import RoutingCase, score

    case = RoutingCase(id="watch_shop_id", request="I want to watch Barbie.", visible_ids=[],
                       operation="play", sequences=[["play"]], target_id="346698")

    def calls(operation):
        return [{"schema": "turn_plan", "output": json.dumps({
            "intent": "control", "request": {"operation": operation, "title": "Barbie", "title_id": "346698"},
            "say": "On it.", "actions": [{"verb": "play", "title_id": "346698"}]})}]
    commands = [{"verb": "play", "args": {"title_id": "346698"}}]
    assert score(case, calls("play"), commands, known_ids={"346698"}) == []
    assert score(case, calls("shop"), commands, known_ids={"346698"}) == [
        "decoded operation ['shop'], expected play"]


def test_routing_score_rejects_fabricated_ids_even_for_a_clarifying_rail():
    import json

    from tools.routing_eval import RoutingCase, score

    case = RoutingCase(id="ambiguous", request="Show the matches", visible_ids=[],
                       sequences=[["show_titles"]], final_intent="clarify")
    action = {"verb": "show_titles", "title_ids": ["invented"], "label": "Which one?"}
    calls = [{"schema": "turn_plan_final", "output": json.dumps({
        "intent": "clarify", "request": {"operation": "play", "title": "Moon", "title_id": None},
        "say": "Which one?", "actions": [action]})}]
    commands = [{"verb": "show_titles", "args": {"title_ids": ["invented"], "label": "Which one?"}}]
    assert score(case, calls, commands, known_ids={"17431"}) == ["unknown title ids: ['invented']"]


async def test_evaluation_budget_prevents_a_second_provider_request():
    import pytest
    from test_agent_service import FakeOpenAI

    from tools.routing_eval import RecordedClient, RequestBudget

    fake = FakeOpenAI(["{}", "{}"])
    client = RecordedClient(fake, RequestBudget(1))
    kwargs = {"messages": [], "response_format": {"json_schema": {"name": "turn_plan"}}}
    await client.create(**kwargs)
    with pytest.raises(RuntimeError, match="budget exhausted"):
        await client.create(**kwargs)
    assert len(fake.calls) == 1


async def test_smoke_tv_cleanup_cancels_a_blocked_handler():
    import pytest
    from conftest import PERSONA

    from tools.smoke_turn import SimulatedTV
    from tv_avatar.control.bus import CommandBus
    from tv_avatar.control.protocol import Playback, ScreenState, Tile
    from tv_avatar.session.state import SessionState

    class BlockedRecorder:
        async def on_screen_transition(self, *args):
            await asyncio.Event().wait()

    session = SessionState("test", "token", 0, persona=PERSONA)
    session.update_screen(ScreenState(view="grid", tiles=[], playback=Playback(state="stopped")))
    tv = SimulatedTV(CommandBus(), session, [Tile(title_id="1", name="One", position=0)],
                     recorder=BlockedRecorder())
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05), tv:
            await tv.bus.dispatch("play", {"title_id": "1"}, "t1")
    assert tv.task.done()
