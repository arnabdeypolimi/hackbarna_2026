"""Process-wide phase-2 components shared by every session.

The catalog, history DB, memory lane and recs engine are per-process (they
own files under data/); sessions and buses are per-connection. Everything
here is optional-by-absence: no catalog parquet → no recs, agent still runs.
"""
import asyncio
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from tv_avatar.config import Settings
from tv_avatar.history.recorder import HistoryRecorder
from tv_avatar.history.store import HistoryStore
from tv_avatar.memory.fake import FakeMemoryLane
from tv_avatar.memory.lane import MemoryLane
from tv_avatar.recs.catalog import CatalogStore
from tv_avatar.recs.embedder import OpenAIEmbedder
from tv_avatar.recs.engine import RecsEngine


@dataclass
class Runtime:
    settings: Settings
    history: HistoryStore
    recorder: HistoryRecorder
    lane: MemoryLane
    catalog: CatalogStore | None = None
    recs: RecsEngine | None = None

    def warm_in_background(self) -> asyncio.Task:
        """Pre-load VoiceMem's local E5 so the first search does not pay ~5 s."""
        async def warm() -> None:
            try:
                await self.lane.warmup()
            except Exception:  # noqa: BLE001 — a cold memory lane is degraded, not fatal
                logger.opt(exception=True).warning("memory lane warmup failed")
        return asyncio.create_task(warm())

    async def close(self) -> None:
        await self.history.close()


def build_runtime(settings: Settings, *, lane: MemoryLane | None = None) -> Runtime:
    history = HistoryStore(settings.history_db_path)
    catalog: CatalogStore | None = None
    recs: RecsEngine | None = None
    if Path(settings.catalog_path).exists() and Path(settings.qdrant_path).exists():
        catalog = CatalogStore(settings.catalog_path, settings.qdrant_path)
        embedder = OpenAIEmbedder(settings.nebius_base_url, settings.nebius_api_key,
                                  settings.embedding_model, settings.embedding_dimensions)
        recs = RecsEngine(catalog, history, embedder, tool_timeout_s=settings.tool_timeout_s)
    else:
        logger.warning("catalog not built; recommendations disabled (run tools/build_catalog.py)",
                       catalog_path=settings.catalog_path)

    if lane is None:
        if settings.agent_impl == "sgr" and settings.nebius_api_key:
            from tv_avatar.memory.voicemem_lane import VoiceMemLane
            lane = VoiceMemLane(settings)
        else:
            lane = FakeMemoryLane()
    return Runtime(settings=settings, history=history, recorder=HistoryRecorder(history, catalog),
                   lane=lane, catalog=catalog, recs=recs)
