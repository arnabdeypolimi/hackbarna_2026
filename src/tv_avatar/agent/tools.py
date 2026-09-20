"""Internal tool dispatch — verbs that never reach the TV (recommend_titles,
reject_title). Every call is bounded by the 400 ms tool budget and degrades
to a spoken fallback instead of hanging the turn.

`run` takes the parsed action (SGR routing: the union member *is* the branch),
never a verb string plus a dict."""
import asyncio
from typing import Any, get_args

from loguru import logger
from opentelemetry import trace
from pydantic import BaseModel

from tv_avatar.agent.envelope import (
    InternalAction,
    RecommendTitles,
    RejectTitle,
)
from tv_avatar.history.recorder import HistoryRecorder
from tv_avatar.recs.catalog import CatalogFilter, CatalogStore
from tv_avatar.recs.engine import RecsContext, RecsEngine
from tv_avatar.tracing import ATTR_ACTION_MATCHED, ATTR_ACTION_N_TITLES


class InternalTools:
    def __init__(self, recs: RecsEngine | None, catalog: CatalogStore | None,
                 *, recorder: HistoryRecorder | None = None, timeout_s: float = 0.4) -> None:
        self._recs = recs
        self._catalog = catalog
        self._recorder = recorder
        self._timeout = timeout_s

    async def run(self, action: InternalAction | BaseModel, user_id: str) -> dict:
        verb = str(getattr(action, "verb", type(action).__name__))
        # A wrong action type is a routing bug, raised before the degraded-answer
        # net below — which must still catch a TypeError thrown *inside* a tool.
        if not isinstance(action, get_args(InternalAction)):
            raise TypeError(f"{verb} is not an internal action")
        try:
            match action:
                case RecommendTitles():
                    return await asyncio.wait_for(self._recommend(action, user_id), timeout=self._timeout)
                case RejectTitle():
                    return self._reject(action, user_id)
        except TimeoutError:
            logger.bind(user_id=user_id).warning("internal tool timed out", verb=verb)
            return {"status": "unavailable", "reason": "timeout"}
        except Exception as err:  # noqa: BLE001 — a tool failure is a degraded answer, not a failed turn
            logger.bind(user_id=user_id).opt(exception=err).warning("internal tool failed", verb=verb)
            return {"status": "error", "reason": type(err).__name__}

    async def _recommend(self, req: RecommendTitles, user_id: str) -> dict:
        if self._recs is None:
            return {"status": "unavailable", "reason": "no catalog"}
        disliked = set(req.exclude_genres)
        constraints = CatalogFilter(
            genres_any=set(req.genres) - disliked, genres_none=disliked,
            year_min=req.year_min, year_max=req.year_max,
        )
        # The memory profile may name titles the viewer declined; embedding it as a
        # retrieval channel would pull exactly those back. Memory reaches the
        # recommender the typed way instead: the model fills genres/exclude_genres.
        ctx = RecsContext(user_id=user_id, query_text=req.query, constraints=constraints,
                          memory_text=None, limit=req.limit)
        log = logger.bind(user_id=user_id)
        log.debug("recommend_titles", step="tool", query=req.query,
                  genres=sorted(constraints.genres_any), excluded_genres=sorted(disliked),
                  year_min=req.year_min, year_max=req.year_max,
                  similar_to=req.similar_to, limit=req.limit)
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
        # With a free-text query, "popular" is the engine's fill-in for nothing having
        # matched it. Those are substitutes, not recommendations: the model is told so.
        # Without a query (a genre or year request) popular-within-the-filters is the
        # genuine answer. Nothing is logged as shown here — the turn records the
        # titles the agent actually went on to name or focus (TurnTrace.offered_ids).
        substitute = bool(req.query) and not req.similar_to
        matched = [t for t in titles if not (substitute and t["why"] == ["popular"])]
        result: dict[str, Any] = {"titles": titles, "matched": bool(matched)}
        # Called inside the `agent.action` span; the engine opens its own below it.
        trace.get_current_span().set_attributes(
            {ATTR_ACTION_N_TITLES: len(titles), ATTR_ACTION_MATCHED: bool(matched)})
        if titles and not matched:
            result["note"] = ("nothing in the catalog matched the request; these are popular fill-ins — "
                              "tell the viewer you could not find a match before offering them")
        return result

    def _reject(self, req: RejectTitle, user_id: str) -> dict:
        logger.bind(user_id=user_id).info("title rejected", step="tool", title_id=req.title_id)
        if self._recorder is not None:
            self._recorder.spawn(self._recorder.on_rec_rejected(user_id, req.title_id))
        return {"status": "ok"}
