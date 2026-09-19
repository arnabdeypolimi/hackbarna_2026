"""Process-wide phase-2 components shared by every session.

The catalog, history DB, memory lane and recs engine are per-process (they
own files under data/); sessions and buses are per-connection. Everything
here is optional-by-absence: no catalog parquet → no recs, agent still runs.
"""
import asyncio
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from tv_avatar import e5
from tv_avatar.config import Settings
from tv_avatar.history.recorder import HistoryRecorder
from tv_avatar.history.store import HistoryStore
from tv_avatar.memory.fake import FakeMemoryLane
from tv_avatar.memory.lane import MemoryLane
from tv_avatar.memory.summary_lane import SummaryMemoryLane
from tv_avatar.recs.catalog import CatalogStore
from tv_avatar.recs.embedder import Embedder, LocalE5Embedder, OpenAIEmbedder
from tv_avatar.recs.engine import RecsEngine


def build_embedder(settings: Settings) -> Embedder:
    if settings.embedding_provider == "local":
        return LocalE5Embedder()
    return OpenAIEmbedder(settings.nebius_base_url, settings.nebius_api_key,
                          settings.embedding_model, settings.embedding_dimensions)


@dataclass
class Runtime:
    settings: Settings
    history: HistoryStore
    recorder: HistoryRecorder
    lane: MemoryLane
    catalog: CatalogStore | None = None
    recs: RecsEngine | None = None

    def warm_in_background(self) -> asyncio.Task:
        """Pre-load the local E5 (the recs query embed has a 240 ms budget; a cold
        model would time every query out into the "popular" fallback) and let the
        memory lane fold in any session that ended without a summary."""
        async def warm() -> None:
            if self.recs is not None and self.settings.embedding_provider == "local":
                try:
                    await asyncio.to_thread(e5.model)
                    logger.info("recs E5 warm")
                except Exception:  # noqa: BLE001 — degraded recs (popular only), not fatal
                    logger.opt(exception=True).warning("recs E5 warmup failed")
            try:
                await self.lane.warmup()
            except Exception:  # noqa: BLE001 — a cold memory lane is degraded, not fatal
                logger.opt(exception=True).warning("memory lane warmup failed")
        return asyncio.create_task(warm())

    async def warm(self) -> None:
        await self.warm_in_background()

    async def close(self) -> None:
        await self.history.close()


def build_runtime(settings: Settings, *, lane: MemoryLane | None = None) -> Runtime:
    history = HistoryStore(settings.history_db_path)
    catalog: CatalogStore | None = None
    recs: RecsEngine | None = None
    if Path(settings.catalog_path).exists() and Path(settings.qdrant_path).exists():
        catalog = CatalogStore(settings.catalog_path, settings.qdrant_path)
        indexed, wanted = catalog.vector_size(), settings.effective_embedding_dimensions
        if indexed is not None and indexed != wanted:
            # A mismatched query would raise inside the first turn that recommends.
            logger.error("catalog index is {}-dim but EMBEDDING_PROVIDER={} embeds {}-dim; "
                         "recommendations disabled — rebuild with tools/build_catalog.py",
                         indexed, settings.embedding_provider, wanted)
        else:
            recs = RecsEngine(catalog, history, build_embedder(settings), tool_timeout_s=settings.tool_timeout_s)
    else:
        logger.warning("catalog not built; recommendations disabled (run tools/build_catalog.py)",
                       catalog_path=settings.catalog_path)

    if lane is None:
        if settings.agent_impl == "sgr" and settings.nebius_api_key:
            lane = SummaryMemoryLane(settings)
        else:
            lane = FakeMemoryLane()
    return Runtime(settings=settings, history=history, recorder=HistoryRecorder(history, catalog),
                   lane=lane, catalog=catalog, recs=recs)
