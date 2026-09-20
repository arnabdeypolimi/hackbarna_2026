"""Turn screen transitions and control-plane messages into history events.

`transition_events` is a pure function of (old, new, runtime_s) so the
completion/abandonment rule is trivially testable."""
import asyncio
from typing import Any, Protocol

from loguru import logger

from tv_avatar.control.protocol import ScreenState
from tv_avatar.history.store import Event, EventKind, HistoryStore

COMPLETION_RATIO = 0.9


class _Catalog(Protocol):
    def lookup(self, title_id: str) -> Any: ...


def transition_events(old: ScreenState | None, new: ScreenState, runtime_s: float | None,
                      user_id: str = "") -> list[Event]:
    old_pb = old.playback if old else None
    new_pb = new.playback
    was_active = old_pb is not None and old_pb.state != "stopped" and old_pb.title_id
    now_active = new_pb.state != "stopped" and new_pb.title_id
    events: list[Event] = []

    ended = was_active and (not now_active or new_pb.title_id != old_pb.title_id)
    if ended:
        watched = old_pb.position_s
        completed = runtime_s is not None and runtime_s > 0 and watched >= COMPLETION_RATIO * runtime_s
        events.append(Event(
            user_id=user_id, title_id=old_pb.title_id,
            kind=EventKind.PLAY_COMPLETED if completed else EventKind.PLAY_ABANDONED,
            detail={"watched_s": watched, "runtime_s": runtime_s},
        ))
    started = now_active and (not was_active or new_pb.title_id != old_pb.title_id)
    if started:
        events.append(Event(user_id=user_id, kind=EventKind.PLAY_STARTED, title_id=new_pb.title_id))
    return events


class HistoryRecorder:
    def __init__(self, store: HistoryStore, catalog: _Catalog | None = None) -> None:
        self._store = store
        self._catalog = catalog

    def _runtime_s(self, title_id: str | None) -> float | None:
        if not title_id or self._catalog is None:
            return None
        item = self._catalog.lookup(title_id)
        return float(item.runtime) * 60 if item is not None and item.runtime else None

    async def on_screen_transition(self, user_id: str, old: ScreenState | None, new: ScreenState) -> None:
        active = old.playback.title_id if old else None
        for event in transition_events(old, new, self._runtime_s(active), user_id=user_id):
            await self._store.record(event)

    async def on_user_event(self, user_id: str, event: str, detail: dict[str, Any]) -> None:
        title_id = detail.get("title_id")
        await self._store.record(Event(user_id=user_id, kind=EventKind.USER_EVENT,
                                       title_id=str(title_id) if title_id else None,
                                       detail={"event": event, **detail}))

    async def on_rec_shown(self, user_id: str, ids: list[str]) -> None:
        for title_id in ids:
            await self._store.record(Event(user_id=user_id, kind=EventKind.REC_SHOWN, title_id=title_id))

    async def on_rec_rejected(self, user_id: str, title_id: str) -> None:
        await self._store.record(Event(user_id=user_id, kind=EventKind.REC_REJECTED, title_id=title_id))

    def spawn(self, coro) -> asyncio.Task:
        """Fire-and-forget with a logged failure — history must never stall a turn."""
        task = asyncio.create_task(coro)
        task.add_done_callback(_log_failure)
        return task


def _log_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.opt(exception=task.exception()).warning("history recording failed")
