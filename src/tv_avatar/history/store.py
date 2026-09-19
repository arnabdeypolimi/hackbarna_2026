"""Event log keyed on user_id (D10). Behavioural signal for recs plus a
"recently watched" line for the prompt.

Callers fire-and-forget via asyncio.create_task — recording must never
stall a turn (same rule as the command bus)."""
import asyncio
import json
import time
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import aiosqlite
from loguru import logger
from pydantic import BaseModel, Field


class EventKind(StrEnum):
    PLAY_STARTED = "play_started"
    PLAY_COMPLETED = "play_completed"
    PLAY_ABANDONED = "play_abandoned"
    FOCUS_DWELL = "focus_dwell"
    SEARCH_ISSUED = "search_issued"
    REC_SHOWN = "rec_shown"
    REC_ACCEPTED = "rec_accepted"
    USER_EVENT = "user_event"


class Event(BaseModel):
    user_id: str
    kind: EventKind
    title_id: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    ts: float = Field(default_factory=time.time)


class _Named(Protocol):
    def lookup(self, title_id: str) -> Any: ...


_WATCHED = (EventKind.PLAY_STARTED, EventKind.PLAY_COMPLETED, EventKind.PLAY_ABANDONED)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    user_id TEXT NOT NULL, ts REAL NOT NULL, kind TEXT NOT NULL,
    title_id TEXT, detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_user_kind ON events(user_id, kind);
"""


class HistoryStore:
    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        self._db: aiosqlite.Connection | None = None
        self._lock: asyncio.Lock | None = None

    async def _conn(self) -> aiosqlite.Connection:
        if self._db is not None:
            return self._db
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._db is None:
                if self._path != ":memory:":
                    Path(self._path).parent.mkdir(parents=True, exist_ok=True)
                db = await aiosqlite.connect(self._path)
                await db.execute("PRAGMA journal_mode=WAL")
                await db.executescript(_SCHEMA)
                await db.commit()
                self._db = db
        return self._db

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def record(self, event: Event) -> None:
        db = await self._conn()
        await db.execute(
            "INSERT INTO events(user_id, ts, kind, title_id, detail_json) VALUES (?, ?, ?, ?, ?)",
            (event.user_id, event.ts, event.kind.value, event.title_id, json.dumps(event.detail)),
        )
        await db.commit()
        logger.bind(user_id=event.user_id).debug("history event", kind=event.kind.value, title_id=event.title_id)

    async def watched_ids(self, user_id: str) -> set[str]:
        db = await self._conn()
        marks = ",".join("?" * len(_WATCHED))
        async with db.execute(
            f"SELECT DISTINCT title_id FROM events WHERE user_id=? AND kind IN ({marks}) AND title_id IS NOT NULL",
            (user_id, *[k.value for k in _WATCHED]),
        ) as cur:
            return {row[0] for row in await cur.fetchall()}

    async def engaged_ids(self, user_id: str, min_watch_s: float = 300) -> list[str]:
        """Titles the user finished or watched for at least `min_watch_s`, most recent first."""
        db = await self._conn()
        async with db.execute(
            "SELECT title_id, kind, detail_json FROM events WHERE user_id=? AND title_id IS NOT NULL "
            "AND kind IN (?, ?) ORDER BY ts DESC",
            (user_id, EventKind.PLAY_COMPLETED.value, EventKind.PLAY_ABANDONED.value),
        ) as cur:
            rows = await cur.fetchall()
        out: list[str] = []
        for title_id, kind, detail_json in rows:
            if title_id in out:
                continue
            if kind == EventKind.PLAY_COMPLETED.value or json.loads(detail_json).get("watched_s", 0) >= min_watch_s:
                out.append(title_id)
        return out

    async def recent_titles(self, user_id: str, limit: int = 10) -> list[str]:
        db = await self._conn()
        async with db.execute(
            "SELECT title_id FROM events WHERE user_id=? AND kind=? AND title_id IS NOT NULL ORDER BY ts DESC",
            (user_id, EventKind.PLAY_STARTED.value),
        ) as cur:
            rows = await cur.fetchall()
        seen: list[str] = []
        for (title_id,) in rows:
            if title_id not in seen:
                seen.append(title_id)
            if len(seen) >= limit:
                break
        return seen

    async def render_for_prompt(self, user_id: str, catalog: _Named | None = None, limit: int = 5) -> str:
        ids = await self.recent_titles(user_id, limit)
        if not ids:
            return "Recently watched: (none yet)"
        names = []
        for title_id in ids:
            item = catalog.lookup(title_id) if catalog else None
            names.append(item.label() if item is not None else f"id={title_id}")
        return "Recently watched: " + "; ".join(names)
