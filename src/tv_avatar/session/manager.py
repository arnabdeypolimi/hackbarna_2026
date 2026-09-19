"""Per-session command bus and pipeline task supervision.

The pipeline and the control socket share the bus; the manager owns the
pipeline task so hang-up and app shutdown can cancel it.
"""
import asyncio
from collections.abc import Coroutine
from typing import Any

from loguru import logger

from tv_avatar.control.bus import CommandBus


class SessionManager:
    def __init__(self) -> None:
        self._buses: dict[str, CommandBus] = {}
        self._pipelines: dict[str, asyncio.Task[None]] = {}

    def bus_for(self, session_id: str) -> CommandBus:
        return self._buses.setdefault(session_id, CommandBus())

    def has(self, session_id: str) -> bool:
        return session_id in self._buses

    def start_pipeline(
        self, session_id: str, coro: Coroutine[Any, Any, None]
    ) -> asyncio.Task[None]:
        """Run one pipeline per session; a second offer replaces the first."""
        self.stop_pipeline(session_id)
        task = asyncio.create_task(coro, name=f"pipeline:{session_id}")
        task.add_done_callback(lambda t: self._on_pipeline_done(session_id, t))
        self._pipelines[session_id] = task
        return task

    def stop_pipeline(self, session_id: str) -> None:
        task = self._pipelines.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()

    def drop(self, session_id: str) -> None:
        self.stop_pipeline(session_id)
        self._buses.pop(session_id, None)

    async def shutdown(self) -> None:
        tasks = list(self._pipelines.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._pipelines.clear()

    def _on_pipeline_done(self, session_id: str, task: asyncio.Task[None]) -> None:
        if self._pipelines.get(session_id) is task:
            self._pipelines.pop(session_id, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("pipeline for {} failed: {!r}", session_id, exc)
