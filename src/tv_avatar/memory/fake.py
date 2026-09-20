"""Test double: canned blocks, recorded calls, optional latency."""
import asyncio

from tv_avatar.memory.lane import BaseMemoryLane, MemoryBlock


class FakeMemoryLane(BaseMemoryLane):
    def __init__(self, blocks: dict[str, MemoryBlock] | None = None, *, search_delay_s: float = 0.0,
                 ingest_delay_s: float = 0.0, fail_ingest: bool = False,
                 recall_budget_s: float | None = None) -> None:
        super().__init__(**({} if recall_budget_s is None else {"recall_budget_s": recall_budget_s}))
        self.blocks = blocks or {}
        self.search_delay_s = search_delay_s
        self.ingest_delay_s = ingest_delay_s
        self.fail_ingest = fail_ingest
        self.searches: list[tuple[str, str]] = []
        self.ingests: list[tuple[str, str, str]] = []
        self.finished: list[str] = []

    async def finish_session(self, user_id: str) -> None:
        self.finished.append(user_id)

    async def _search(self, user_id: str, text: str) -> MemoryBlock:
        self.searches.append((user_id, text))
        if self.search_delay_s:
            await asyncio.sleep(self.search_delay_s)
        return self.blocks.get(user_id, MemoryBlock())

    async def _ingest(self, user_id: str, user_text: str, assistant_text: str) -> dict:
        self.ingests.append((user_id, user_text, assistant_text))
        if self.ingest_delay_s:
            await asyncio.sleep(self.ingest_delay_s)
        if self.fail_ingest:
            raise RuntimeError("ingest exploded")
        return {"facts": [user_text], "memory_ids": [f"m_{len(self.ingests)}"]}
