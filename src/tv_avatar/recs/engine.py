"""Retrieve → hard-filter → re-rank → return catalog IDs only.

Three candidate channels — query match, taste (mean of engaged vectors),
popularity — merged by title. The query embedding is the only network hop
and it is speculative: warmed from STT partials and bounded by the tool
timeout, never awaited naked on the turn.
"""
import asyncio
import time

from loguru import logger
from pydantic import BaseModel, Field

from tv_avatar.history.store import HistoryStore
from tv_avatar.recs.catalog import CatalogFilter, CatalogItem, CatalogStore
from tv_avatar.recs.embedder import Embedder

W_MATCH = 1.0
W_TASTE = 0.6
W_MEMORY = 0.5
W_POPULARITY = 0.2
CHANNEL_LIMIT = 50
PREFIX_CHARS = 20
QUERY_CACHE_TTL_S = 30.0
DEFAULT_MIN_VOTES = 50
#: The embed hop gets this share of the tool budget so a timeout still leaves
#: room for the taste/popular channels to answer inside the same budget.
EMBED_BUDGET_FRACTION = 0.6


class RecsContext(BaseModel):
    user_id: str
    query_text: str | None = None
    constraints: CatalogFilter = Field(default_factory=CatalogFilter)
    memory_text: str | None = None
    limit: int = 8


class RecoItem(BaseModel):
    title_id: str
    name: str
    score: float
    reasons: list[str] = Field(default_factory=list)


def _norm_query(text: str) -> str:
    return " ".join(text.lower().split())[:PREFIX_CHARS]


async def _none() -> None:
    return None


def _mean(vectors: list[list[float]]) -> list[float]:
    n = len(vectors)
    return [sum(col) / n for col in zip(*vectors, strict=True)]


class RecsEngine:
    def __init__(self, catalog: CatalogStore, history: HistoryStore, embedder: Embedder,
                 *, tool_timeout_s: float = 0.4) -> None:
        self._catalog = catalog
        self._history = history
        self._embedder = embedder
        self._timeout = tool_timeout_s * EMBED_BUDGET_FRACTION
        self._query_cache: dict[tuple[str, str], tuple[float, list[float]]] = {}
        self._inflight: dict[tuple[str, str], asyncio.Task] = {}
        self._taste_cache: dict[str, list[float] | None] = {}
        self._pop_max = max((i.popularity for i in catalog.top_popular(1)), default=1.0) or 1.0

    # --- speculative query embedding ------------------------------------

    def prefetch_query(self, user_id: str, partial_text: str) -> None:
        """Called from the STT-partials tap. Never awaited by the caller."""
        key = (user_id, _norm_query(partial_text))
        if not key[1] or key in self._query_cache or key in self._inflight:
            return
        task = asyncio.create_task(self._embed_and_cache(key, partial_text))
        self._inflight[key] = task
        task.add_done_callback(lambda _t, k=key: self._inflight.pop(k, None))

    async def _embed_and_cache(self, key: tuple[str, str], text: str) -> list[float]:
        vec = (await self._embedder.embed([text]))[0]
        self._query_cache[key] = (time.monotonic(), vec)
        return vec

    async def _query_vector(self, ctx: RecsContext) -> list[float] | None:
        assert ctx.query_text is not None
        key = (ctx.user_id, _norm_query(ctx.query_text))
        log = logger.bind(user_id=ctx.user_id)
        hit = self._query_cache.get(key)
        if hit and time.monotonic() - hit[0] < QUERY_CACHE_TTL_S:
            log.debug("query embed cache hit")
            return hit[1]
        pending = self._inflight.get(key)
        try:
            if pending is not None:
                return await asyncio.wait_for(asyncio.shield(pending), timeout=self._timeout)
            return await asyncio.wait_for(self._embed_and_cache(key, ctx.query_text), timeout=self._timeout)
        except TimeoutError:
            log.debug("query embed timeout; falling back to taste/popular")
            return None
        except Exception as err:  # noqa: BLE001 — a rec is degraded, never a failed turn
            log.warning("query embed failed", error=type(err).__name__)
            return None

    # --- taste ------------------------------------------------------------

    async def _taste_vector(self, user_id: str) -> list[float] | None:
        if user_id in self._taste_cache:
            return self._taste_cache[user_id]
        engaged = await self._history.engaged_ids(user_id)
        vectors = list(self._catalog.vectors(engaged[:20]).values()) if engaged else []
        taste = _mean(vectors) if vectors else None
        self._taste_cache[user_id] = taste
        return taste

    async def invalidate_taste(self, user_id: str) -> None:
        self._taste_cache.pop(user_id, None)

    # --- public -----------------------------------------------------------

    async def recommend(self, ctx: RecsContext) -> list[RecoItem]:
        t0 = time.perf_counter()
        watched = await self._history.watched_ids(ctx.user_id)
        base = ctx.constraints
        filters = base.model_copy(update={
            "exclude_ids": base.exclude_ids | watched,
            "min_vote_count": base.min_vote_count if base.min_vote_count is not None else DEFAULT_MIN_VOTES,
        })

        # The two network hops (query embed, memory embed) run concurrently so the
        # worst case is one embed budget, not two — the taste channel is local.
        qv, mv, taste = await asyncio.gather(
            self._query_vector(ctx) if ctx.query_text else _none(),
            self._safe_embed(ctx.memory_text) if ctx.memory_text else _none(),
            self._taste_vector(ctx.user_id),
        )
        channels: list[tuple[list[tuple[CatalogItem, float]], float, str]] = []
        if qv is not None:
            channels.append((self._catalog.search(qv, filters, CHANNEL_LIMIT), W_MATCH, "match"))
        if taste is not None:
            channels.append((self._catalog.search(taste, filters, CHANNEL_LIMIT), W_TASTE, "for you"))
        if mv is not None:
            channels.append((self._catalog.search(mv, filters, CHANNEL_LIMIT), W_MEMORY, "from what you told me"))
        if not channels:
            popular = self._catalog.top_popular(ctx.limit * 3, exclude=filters.exclude_ids)
            channels.append(([(i, 0.0) for i in popular if self._passes(i, filters)], 1.0, "popular"))

        recs = self._merge(channels)[: ctx.limit]
        logger.bind(user_id=ctx.user_id).debug(
            "recommend", step="recs", n=len(recs), excluded_watched=len(watched),
            channels={tag: len(hits) for hits, _, tag in channels},
            query_vec=qv is not None, taste_vec=taste is not None, memory_vec=mv is not None,
            ms=round((time.perf_counter() - t0) * 1000, 1),
        )
        return recs

    async def similar(self, title_id: str, ctx: RecsContext) -> list[RecoItem]:
        vec = self._catalog.vectors([title_id]).get(title_id)
        if vec is None:
            return await self.recommend(ctx)
        watched = await self._history.watched_ids(ctx.user_id)
        filters = ctx.constraints.model_copy(update={
            "exclude_ids": ctx.constraints.exclude_ids | watched | {title_id}
        })
        anchor = self._catalog.lookup(title_id)
        tag = f"like {anchor.name}" if anchor else "similar"
        return self._merge([(self._catalog.search(vec, filters, CHANNEL_LIMIT), W_MATCH, tag)])[: ctx.limit]

    # --- helpers ----------------------------------------------------------

    async def _safe_embed(self, text: str) -> list[float] | None:
        try:
            return (await asyncio.wait_for(self._embedder.embed([text]), timeout=self._timeout))[0]
        except (TimeoutError, Exception):  # noqa: BLE001
            return None

    def _passes(self, item: CatalogItem, f: CatalogFilter) -> bool:
        return (
            (not f.genres_any or bool(f.genres_any & set(item.genres)))
            and not (f.genres_none & set(item.genres))
            and (f.year_min is None or (item.year or 0) >= f.year_min)
            and (f.year_max is None or (item.year or 9999) <= f.year_max)
            and (not f.languages or item.original_language in f.languages)
            and (f.min_vote_count is None or item.vote_count >= f.min_vote_count)
        )

    def _merge(self, channels: list[tuple[list[tuple[CatalogItem, float]], float, str]]) -> list[RecoItem]:
        merged: dict[str, RecoItem] = {}
        for hits, weight, tag in channels:
            for item, sim in hits:
                score = weight * sim + W_POPULARITY * (item.popularity / self._pop_max)
                prev = merged.get(item.title_id)
                if prev is None:
                    merged[item.title_id] = RecoItem(title_id=item.title_id, name=item.name,
                                                     score=score, reasons=[tag])
                else:
                    prev.score = max(prev.score, score)
                    if tag not in prev.reasons:
                        prev.reasons.append(tag)
        return sorted(merged.values(), key=lambda r: -r.score)
