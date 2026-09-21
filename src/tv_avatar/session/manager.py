"""Per-session command bus and pipeline task supervision.

The pipeline and the control socket share the bus; the manager owns the
pipeline task so hang-up and app shutdown can cancel it.
"""
import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from loguru import logger

from tv_avatar.control.bus import CommandBus

#: Hands the running pipeline a viewer utterance that did not come from the
#: microphone. Deliberately an opaque callable: the manager supervises sessions
#: and must not learn about Pipecat frames to do it.
TextInjector = Callable[[str], Awaitable[None]]


class SessionManager:
    def __init__(self) -> None:
        self._buses: dict[str, CommandBus] = {}
        self._pipelines: dict[str, asyncio.Task[None]] = {}
        self._injectors: dict[str, TextInjector] = {}

    def bus_for(self, session_id: str) -> CommandBus:
        return self._buses.setdefault(session_id, CommandBus())

    def has(self, session_id: str) -> bool:
        return session_id in self._buses

    def injector_for(self, session_id: str) -> TextInjector | None:
        """How to speak for the viewer, or None while no pipeline is running."""
        return self._injectors.get(session_id)

    def injector_slot(self, session_id: str) -> Callable[[TextInjector | None], None]:
        """One pipeline's handle for publishing its text entry point, and None to
        withdraw it.

        The withdrawal is conditional because teardown is not ordered against
        startup: a replaced pipeline reaches its `finally` after its successor has
        already published, and an unconditional delete there would leave the live
        session with no way in.
        """
        mine: list[TextInjector] = []

        def publish(inject: TextInjector | None) -> None:
            if inject is not None:
                mine.append(inject)
                self._injectors[session_id] = inject
            elif mine and self._injectors.get(session_id) is mine[-1]:
                del self._injectors[session_id]

        return publish

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
        # Unpublished before the cancellation it cannot outlive: a pipeline being
        # torn down must stop accepting utterances immediately, not once its
        # teardown gets around to withdrawing itself.
        self._injectors.pop(session_id, None)
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
