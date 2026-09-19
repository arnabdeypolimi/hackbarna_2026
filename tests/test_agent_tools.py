"""Internal tools: what the model is told about results, and what reaches history."""
import asyncio

from tv_avatar.agent.tools import InternalTools
from tv_avatar.history.recorder import HistoryRecorder
from tv_avatar.history.store import EventKind, HistoryStore
from tv_avatar.memory.fake import FakeMemoryLane
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
    tools = InternalTools(FakeRecs(items), FakeMemoryLane(), None, recorder=HistoryRecorder(store))
    return tools, store


async def _shown(store, user="u1"):
    await asyncio.sleep(0.02)  # recorder writes are fire-and-forget
    return [t for t, _ in await store.recent_recommended(user)]


async def test_matched_results_are_logged_as_shown(tmp_path):
    tools, store = await _tools(tmp_path, [RecoItem(title_id="1", name="Heat", score=0.9, reasons=["match"]),
                                           RecoItem(title_id="2", name="Drive", score=0.3, reasons=["popular"])])
    result = await tools.run("recommend_titles", {"query": "heist"}, "u1", None)
    assert result["matched"] is True and "note" not in result
    assert [t["name"] for t in result["titles"]] == ["Heat", "Drive"]
    assert await _shown(store) == ["1"]          # the popular fill-in is not a recommendation
    await store.close()


async def test_popular_only_results_are_flagged_and_not_logged(tmp_path):
    tools, store = await _tools(tmp_path, [RecoItem(title_id="9", name="Barbie", score=0.07, reasons=["popular"])])
    result = await tools.run("recommend_titles", {"query": "Office"}, "u1", None)
    assert result["matched"] is False
    assert "could not find a match" in result["note"]
    assert result["titles"][0]["name"] == "Barbie"   # still offered, but honestly
    assert await _shown(store) == []
    await store.close()


async def test_genre_only_request_treats_popular_within_filters_as_a_real_answer(tmp_path):
    tools, store = await _tools(tmp_path, [RecoItem(title_id="7", name="Saw X", score=0.1, reasons=["popular"])])
    result = await tools.run("recommend_titles", {"genres": ["Horror"]}, "u1", None)
    assert result["matched"] is True and "note" not in result   # nothing to "match": no free-text query
    assert await _shown(store) == ["7"]
    await store.close()


async def test_reject_title_records_history_without_awaiting_a_second_cycle(tmp_path):
    from tv_avatar.agent.envelope import AWAITED_VERBS, INTERNAL_AWAIT

    tools, store = await _tools(tmp_path, [])
    result = await tools.run("reject_title", {"title_id": "346698"}, "u1", None)
    assert result == {"status": "ok"}
    await asyncio.sleep(0.02)
    events = await store.recent_events("u1", EventKind.REC_REJECTED)
    assert [e.title_id for e in events] == ["346698"]
    assert await store.rejected_ids("u1") == {"346698"}
    assert "reject_title" not in INTERNAL_AWAIT and "reject_title" not in AWAITED_VERBS
    await store.close()
