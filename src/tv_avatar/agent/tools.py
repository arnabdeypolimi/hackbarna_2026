"""Internal tool dispatch — verbs that never reach the TV (recommend_titles,
recall_memory). Every call is bounded by the 400 ms tool budget and degrades
to a spoken fallback instead of hanging the turn."""
import asyncio
from typing import Any

from loguru import logger

from tv_avatar.agent.envelope import RecallMemory, RecommendTitles
from tv_avatar.history.recorder import HistoryRecorder
from tv_avatar.memory.lane import MemoryLane
from tv_avatar.recs.catalog import CatalogFilter, CatalogStore
from tv_avatar.recs.engine import RecsContext, RecsEngine


class InternalTools:
    def __init__(self, recs: RecsEngine | None, lane: MemoryLane, catalog: CatalogStore | None,
                 *, recorder: HistoryRecorder | None = None, timeout_s: float = 0.4) -> None:
        self._recs = recs
        self._lane = lane
        self._catalog = catalog
        self._recorder = recorder
        self._timeout = timeout_s

    async def run(self, verb: str, args: dict[str, Any], user_id: str, memory_text: str | None) -> dict:
        try:
            match verb:
                case "recommend_titles":
                    return await asyncio.wait_for(
                        self._recommend(RecommendTitles.model_validate(args), user_id, memory_text),
                        timeout=self._timeout)
                case "recall_memory":
                    return await asyncio.wait_for(
                        self._recall(RecallMemory.model_validate(args), user_id), timeout=self._timeout)
                case _:
                    return {"status": "unknown_tool", "verb": verb}
        except TimeoutError:
            logger.bind(user_id=user_id).warning("internal tool timed out", verb=verb)
            return {"status": "unavailable", "reason": "timeout"}
        except Exception as err:  # noqa: BLE001 — a tool failure is a degraded answer, not a failed turn
            logger.bind(user_id=user_id).opt(exception=err).warning("internal tool failed", verb=verb)
            return {"status": "error", "reason": type(err).__name__}

    async def _recommend(self, req: RecommendTitles, user_id: str, memory_text: str | None) -> dict:
        if self._recs is None:
            return {"status": "unavailable", "reason": "no catalog"}
        disliked = set(req.exclude_genres)
        constraints = CatalogFilter(
            genres_any=set(req.genres) - disliked, genres_none=disliked,
            year_min=req.year_min, year_max=req.year_max,
        )
        ctx = RecsContext(user_id=user_id, query_text=req.query, constraints=constraints,
                          memory_text=memory_text, limit=req.limit)
        log = logger.bind(user_id=user_id)
        log.debug("recommend_titles", step="tool", query=req.query,
                  genres=sorted(constraints.genres_any), excluded_genres=sorted(disliked),
                  year_min=req.year_min, year_max=req.year_max,
                  similar_to=req.similar_to, limit=req.limit, has_memory=memory_text is not None)
        if req.similar_to:
            recs = await self._recs.similar(req.similar_to, ctx)
        else:
            recs = await self._recs.recommend(ctx)
        log.debug("recommend_titles result", step="tool",
                  titles=[(r.title_id, r.name, round(r.score, 3), r.reasons) for r in recs])
        titles = []
        for r in recs:
            item = self._catalog.lookup(r.title_id) if self._catalog else None
            titles.append({
                "title_id": r.title_id, "name": r.name,
                "year": item.year if item else None,
                "genres": item.genres[:3] if item else [],
                "why": r.reasons,
            })
        if self._recorder is not None and titles:
            self._recorder.spawn(self._recorder.on_rec_shown(user_id, [t["title_id"] for t in titles]))
        return {"titles": titles}

    async def _recall(self, req: RecallMemory, user_id: str) -> dict:
        block = await self._lane.recall(user_id, req.query)
        logger.bind(user_id=user_id).debug("recall_memory", step="tool", query=req.query,
                                           empty=block.empty, tokens=block.token_est)
        return {"memory": block.render_for_prompt()}
