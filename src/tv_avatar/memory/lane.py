"""MemoryLane protocol + the speculative prefetch/recall semantics both
implementations share.

recall(): if a prefetch for this user finished within PREFETCH_TTL_S and its
query prefix-matches the final transcript, reuse it (the speculative win);
otherwise search fresh. Nothing here may block the turn beyond one search."""
import asyncio
import contextlib
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Protocol

from loguru import logger
from pydantic import BaseModel

PREFETCH_TTL_S = 2.0
PREFIX_CHARS = 20
MAX_RENDER_CHARS = 1800  # ≈ 450–500 tokens


class MemoryBlock(BaseModel):
    left: str = ""
    right: str = ""
    speaker_id: str | None = None
    token_est: int = 0
    stale: bool = False

    @property
    def empty(self) -> bool:
        return not (self.left.strip() or self.right.strip())

    def render_for_prompt(self) -> str:
        if self.empty:
            return "(none yet)"
        parts = []
        if self.left.strip():
            parts.append(f"Known facts and preferences:\n{self.left.strip()}")
        if self.right.strip():
            parts.append(f"Persona and mood cues:\n{self.right.strip()}")
        text = "\n".join(parts)
        if len(text) > MAX_RENDER_CHARS:
            text = text[:MAX_RENDER_CHARS].rsplit("\n", 1)[0] + "\n…"
        return text

    @classmethod
    def from_lines(cls, left: list[str], right: list[str], speaker_id: str | None = None) -> "MemoryBlock":
        left_text = "\n".join(f"- {line}" for line in left if line)
        right_text = "\n".join(f"- {line}" for line in right if line)
        return cls(left=left_text, right=right_text, speaker_id=speaker_id,
                   token_est=(len(left_text) + len(right_text)) // 4)


class MemoryLane(Protocol):
    async def prefetch(self, user_id: str, partial: str) -> None: ...
    async def recall(self, user_id: str, final: str) -> MemoryBlock: ...
    async def ingest_turn(self, user_id: str, user_text: str, assistant_text: str) -> None: ...
    async def warmup(self) -> None: ...


def _prefix(text: str) -> str:
    return " ".join(text.lower().split())[:PREFIX_CHARS]


@dataclass
class _Prefetched:
    prefix: str
    done_at: float
    block: MemoryBlock


class BaseMemoryLane(ABC):
    """Prefetch cache + error containment; subclasses supply `_search`/`_ingest`."""

    def __init__(self) -> None:
        self._prefetched: dict[str, _Prefetched] = {}
        self._inflight: dict[str, asyncio.Task[MemoryBlock]] = {}

    @abstractmethod
    async def _search(self, user_id: str, text: str) -> MemoryBlock: ...

    @abstractmethod
    async def _ingest(self, user_id: str, user_text: str, assistant_text: str) -> dict: ...

    async def warmup(self) -> None:
        return None

    async def prefetch(self, user_id: str, partial: str) -> None:
        prefix = _prefix(partial)
        if not prefix:
            return
        cached = self._prefetched.get(user_id)
        if cached and cached.prefix == prefix and time.monotonic() - cached.done_at < PREFETCH_TTL_S:
            return
        pending = self._inflight.get(user_id)
        if pending is not None and not pending.done():
            pending.cancel()
        task = asyncio.create_task(self._prefetch_task(user_id, prefix, partial))
        self._inflight[user_id] = task
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _prefetch_task(self, user_id: str, prefix: str, text: str) -> MemoryBlock:
        log = logger.bind(user_id=user_id)
        t0 = time.perf_counter()
        try:
            block = await self._search(user_id, text)
        except Exception as err:  # noqa: BLE001 — memory is best-effort on the turn
            log.warning("memory prefetch failed", error=type(err).__name__)
            block = MemoryBlock(stale=True)
        self._prefetched[user_id] = _Prefetched(prefix, time.monotonic(), block)
        log.debug("memory prefetch done", ms=round((time.perf_counter() - t0) * 1000, 1))
        return block

    async def recall(self, user_id: str, final: str) -> MemoryBlock:
        log = logger.bind(user_id=user_id)
        prefix = _prefix(final)
        pending = self._inflight.get(user_id)
        if pending is not None and not pending.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):  # logged by the prefetch task
                await asyncio.shield(pending)
        cached = self._prefetched.get(user_id)
        if cached is not None:
            age = time.monotonic() - cached.done_at
            if age < PREFETCH_TTL_S and (prefix.startswith(cached.prefix) or cached.prefix.startswith(prefix)):
                log.debug("memory prefetch hit", age_ms=round(age * 1000))
                return cached.block
        log.debug("memory prefetch miss")
        try:
            return await self._search(user_id, final)
        except Exception as err:  # noqa: BLE001
            log.warning("memory recall failed", error=type(err).__name__)
            return MemoryBlock(stale=True)

    async def ingest_turn(self, user_id: str, user_text: str, assistant_text: str) -> None:
        log = logger.bind(user_id=user_id)
        if not user_text.strip():
            return
        try:
            result = await self._ingest(user_id, user_text, assistant_text)
        except Exception:  # noqa: BLE001 — visible in the log, invisible to the conversation
            log.opt(exception=True).warning("memory ingest failed")
            return
        facts = result.get("facts") or result.get("results") or []
        log.info("memory ingest ok", facts_count=len(facts) if hasattr(facts, "__len__") else 0,
                 memory_ids=result.get("memory_ids") or result.get("ids") or [])
        log.debug("memory ingest detail", result=result)
