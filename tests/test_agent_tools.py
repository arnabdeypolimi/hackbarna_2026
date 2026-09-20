"""Internal tools: what the model is told about results, and what reaches history."""
import asyncio

import pytest

from tv_avatar.agent.commands import Focus
from tv_avatar.agent.envelope import RecommendTitles, RejectTitle
from tv_avatar.agent.tools import InternalTools
from tv_avatar.history.recorder import HistoryRecorder
from tv_avatar.history.store import EventKind, HistoryStore
from tv_avatar.recs.engine import RecoItem


class FakeRecs:
    def __init__(self, items: list[RecoItem]) -> None:
        self.items = items

    async def recommend(self, ctx):
        return self.items

    async def similar(self, title_id, ctx):
        return self.items


async def _tools(tmp_path, items):
    store = HistoryStore(str(tmp_path / "h.db"))
    tools = InternalTools(FakeRecs(items), None, recorder=HistoryRecorder(store))
    return tools, store


async def _shown(store, user="u1"):
    await asyncio.sleep(0.02)  # recorder writes are fire-and-forget
    return [t for t, _ in await store.recent_recommended(user)]


async def test_tool_returns_candidates_without_logging_them_as_shown(tmp_path):
    """The tool cannot know which of its titles the agent will go on to name;
    the turn records rec_shown from what was actually said (see test_agent_service)."""
    tools, store = await _tools(tmp_path, [RecoItem(title_id="1", name="Heat", score=0.9, reasons=["match"])])
    result = await tools.run(RecommendTitles(query="heist"), "u1")
    assert [t["title_id"] for t in result["titles"]] == ["1"]
    assert await _shown(store) == []
    await store.close()


async def test_matched_results_are_not_flagged(tmp_path):
    tools, store = await _tools(tmp_path, [RecoItem(title_id="1", name="Heat", score=0.9, reasons=["match"]),
                                           RecoItem(title_id="2", name="Drive", score=0.3, reasons=["popular"])])
    result = await tools.run(RecommendTitles(query="heist"), "u1")
    assert result["matched"] is True and "note" not in result
    assert [t["name"] for t in result["titles"]] == ["Heat", "Drive"]
    await store.close()


async def test_popular_only_results_are_flagged(tmp_path):
    tools, store = await _tools(tmp_path, [RecoItem(title_id="9", name="Barbie", score=0.07, reasons=["popular"])])
    result = await tools.run(RecommendTitles(query="Office"), "u1")
    assert result["matched"] is False
    assert "could not find a match" in result["note"]
    assert result["titles"][0]["name"] == "Barbie"   # still offered, but honestly
    await store.close()


async def test_genre_only_request_treats_popular_within_filters_as_a_real_answer(tmp_path):
    tools, store = await _tools(tmp_path, [RecoItem(title_id="7", name="Saw X", score=0.1, reasons=["popular"])])
    result = await tools.run(RecommendTitles(genres=["Horror"]), "u1")
    assert result["matched"] is True and "note" not in result   # nothing to "match": no free-text query
    await store.close()


async def test_reject_title_records_history_without_awaiting_a_second_cycle(tmp_path):
    from tv_avatar.agent.envelope import REGISTRY

    tools, store = await _tools(tmp_path, [])
    result = await tools.run(RejectTitle(title_id="346698"), "u1")
    assert result == {"status": "ok"}
    await asyncio.sleep(0.02)
    events = await store.recent_events("u1", EventKind.REC_REJECTED)
    assert [e.title_id for e in events] == ["346698"]
    assert await store.rejected_ids("u1") == {"346698"}
    assert not REGISTRY["reject_title"].awaits_result and not REGISTRY["reject_title"].returns_observation
    await store.close()


async def test_run_routes_on_the_typed_action_not_a_verb_string(tmp_path):
    """SGR routing: the parsed union member picks the branch. A TV command is a
    programming error here, not a degraded result."""
    tools, store = await _tools(tmp_path, [])
    with pytest.raises(TypeError, match="not an internal action"):
        await tools.run(Focus(title_id="1"), "u1")
    await store.close()
