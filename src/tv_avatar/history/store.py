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
    REC_SHOWN = "rec_shown"
    #: The viewer declined a title the agent offered ("no, not that one").
    REC_REJECTED = "rec_rejected"
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


def _when(ts: float, now: float | None = None) -> str:
    """'just now' / '2 hours ago' / 'yesterday' / '3 days ago' — spoken-friendly."""
    age = (now if now is not None else time.time()) - ts
    if age < 120:
        return "just now"
    if age < 3600:
        return f"{int(age // 60)} minutes ago"
    if age < 86400:
        hours = int(age // 3600)
        return "an hour ago" if hours == 1 else f"{hours} hours ago"
    days = int(age // 86400)
    return "yesterday" if days == 1 else f"{days} days ago"


# Every read below is "this user's events of kind K, newest first", so ts is in
# the index; the older (user_id, kind) index is a strict prefix and is dropped.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    user_id TEXT NOT NULL, ts REAL NOT NULL, kind TEXT NOT NULL,
    title_id TEXT, detail_json TEXT NOT NULL DEFAULT '{}'
);
DROP INDEX IF EXISTS idx_events_user_kind;
CREATE INDEX IF NOT EXISTS idx_events_user_kind_ts ON events(user_id, kind, ts DESC);
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
        await self.record_many([event])

    async def record_many(self, events: list[Event]) -> None:
        """One transaction for a batch — `rec_shown` writes a row per recommended title."""
        if not events:
            return
        db = await self._conn()
        await db.executemany(
            "INSERT INTO events(user_id, ts, kind, title_id, detail_json) VALUES (?, ?, ?, ?, ?)",
            [(e.user_id, e.ts, e.kind.value, e.title_id, json.dumps(e.detail)) for e in events],
        )
        await db.commit()
        for event in events:
            logger.bind(user_id=event.user_id).debug("history event", kind=event.kind.value, title_id=event.title_id)

    async def watched_ids(self, user_id: str) -> set[str]:
        db = await self._conn()
        marks = ",".join("?" * len(_WATCHED))
        async with db.execute(
            f"SELECT DISTINCT title_id FROM events WHERE user_id=? AND kind IN ({marks}) AND title_id IS NOT NULL",
            (user_id, *[k.value for k in _WATCHED]),
        ) as cur:
            return {row[0] for row in await cur.fetchall()}

    async def engaged_ids(self, user_id: str, min_watch_s: float = 300, limit: int = 20) -> list[str]:
        """Titles the user finished or watched for at least `min_watch_s`, most recent first."""
        db = await self._conn()
        async with db.execute(
            "SELECT title_id FROM events WHERE user_id=? AND title_id IS NOT NULL AND "
            "(kind=? OR (kind=? AND json_extract(detail_json, '$.watched_s') >= ?)) "
            "GROUP BY title_id ORDER BY MAX(ts) DESC LIMIT ?",
            (user_id, EventKind.PLAY_COMPLETED.value, EventKind.PLAY_ABANDONED.value, min_watch_s, limit),
        ) as cur:
            return [row[0] for row in await cur.fetchall()]

    async def recent_titles(self, user_id: str, limit: int = 10) -> list[str]:
        db = await self._conn()
        async with db.execute(
            "SELECT title_id FROM events WHERE user_id=? AND kind=? AND title_id IS NOT NULL "
            "GROUP BY title_id ORDER BY MAX(ts) DESC LIMIT ?",
            (user_id, EventKind.PLAY_STARTED.value, limit),
        ) as cur:
            return [row[0] for row in await cur.fetchall()]

    async def rejected_ids(self, user_id: str) -> set[str]:
        """Titles the viewer declined and has not played since — kept out of
        recommendations and the greeting until they do."""
        db = await self._conn()
        # A later play forgives an earlier rejection: the latest rejection must be
        # newer than the latest play of the same title (or there is no play at all).
        async with db.execute(
            "SELECT r.title_id FROM events r WHERE r.user_id=? AND r.kind=? AND r.title_id IS NOT NULL "
            "GROUP BY r.title_id HAVING MAX(r.ts) > COALESCE((SELECT MAX(p.ts) FROM events p "
            "WHERE p.user_id=r.user_id AND p.title_id=r.title_id AND p.kind=?), -1)",
            (user_id, EventKind.REC_REJECTED.value, EventKind.PLAY_STARTED.value),
        ) as cur:
            return {row[0] for row in await cur.fetchall()}

    async def recent_events(self, user_id: str, kind: EventKind, limit: int = 20) -> list[Event]:
        db = await self._conn()
        async with db.execute(
            "SELECT ts, title_id, detail_json FROM events WHERE user_id=? AND kind=? ORDER BY ts DESC LIMIT ?",
            (user_id, kind.value, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [Event(user_id=user_id, kind=kind, title_id=t, detail=json.loads(d), ts=ts) for ts, t, d in rows]

    async def recent_recommended(self, user_id: str, limit: int = 6) -> list[tuple[str, float]]:
        """Titles the agent recommended to this user and they did not decline,
        newest first, with the event time."""
        db = await self._conn()
        skip = await self.rejected_ids(user_id)
        # Over-fetch by the rejected count so the LIMIT still fills after filtering.
        async with db.execute(
            "SELECT title_id, MAX(ts) FROM events WHERE user_id=? AND kind=? AND title_id IS NOT NULL "
            "GROUP BY title_id ORDER BY MAX(ts) DESC LIMIT ?",
            (user_id, EventKind.REC_SHOWN.value, limit + len(skip)),
        ) as cur:
            rows = await cur.fetchall()
        return [(title_id, ts) for title_id, ts in rows if title_id not in skip][:limit]

    async def render_for_prompt(self, user_id: str, catalog: _Named | None = None, limit: int = 5) -> str:
        """Episodic memory for the prompt: what was watched and what was recommended
        (with when). Durable preferences live in the memory profile; this is the event log."""
        def name(title_id: str, *suffix: str) -> str:
            """`The Dark Knight (2008) — Action (id=155, yesterday)` — the id rides along
            because the agent may only emit ids it can see in the prompt."""
            item = catalog.lookup(title_id) if catalog else None
            label = item.label() if item is not None else "unknown title"
            return f"{label} ({', '.join((f'id={title_id}', *suffix))})"

        watched = await self.recent_titles(user_id, limit)
        recommended = await self.recent_recommended(user_id, limit + 1)
        lines = ["Recently watched: " + ("; ".join(name(t) for t in watched) if watched else "(none yet)")]
        if recommended:
            lines.append("Recently recommended: " + "; ".join(name(t, _when(ts)) for t, ts in recommended))
        return "\n".join(lines)
