"""Long-term memory as one rolling profile per user (D6, revised 2026-09-19).

Per turn nothing is inferred: the exchange is appended to the user's pending
transcript. When the session ends (`finish_session`, fired from the pipeline
runner) one LLM call rewrites `profile.md` from the previous profile plus the
transcript; the next session reads that file once. A transcript left behind by
a crash is folded in at the next `warmup()`.

The profile is about the viewer — genres, moods, pacing, how they like to be
talked to, and titles when they carry a signal (enjoyed, declined and why).
The viewing log in history.db stays the source of truth for what was watched,
offered or declined: the greeting and the recommender take titles from it,
never from here, so a stray line can shade the tone but not pick the film.
"""
import asyncio
import json
import time
from pathlib import Path
from typing import Any

from loguru import logger
from openai import AsyncOpenAI

from tv_avatar.config import Settings
from tv_avatar.memory.lane import BaseMemoryLane, MemoryBlock

PROFILE_FILE = "profile.md"
PENDING_FILE = "pending.jsonl"
SESSIONS_DIR = "sessions"
#: Transcript characters handed to the summariser; older turns are dropped first.
MAX_TRANSCRIPT_CHARS = 12_000

SUMMARY_SYSTEM = """\
You maintain a short profile of one TV viewer for a voice assistant that lives on their TV.
You receive the previous profile and the transcript of the session that just ended.
Rewrite the profile so it is the best possible briefing for the assistant's next session.

Write at most {max_words} words, plain Markdown, exactly these sections (omit a section
if it would be empty):
## Preferences — genres, moods, pacing, eras, languages, what they enjoy
## Dislikes — what to avoid and why, when they said so
## How to talk to them — tone, length, humour, how they correct you, what annoyed them
## Open threads — unfinished decisions or promises worth picking up next time

Rules:
- Titles are welcome when they carry a signal about the viewer: what they enjoyed, what they
  declined and why ("declined The Nun II — too jump-scary", "loved the pacing of Suzume").
  The TV keeps its own exact log of what was watched, offered and declined, so do not list
  titles for their own sake, and never write down a title the assistant merely suggested as
  something the viewer wants. Attribute a like or dislike to a title only when the viewer
  named or pointed at that specific title ("not the first one" = the first title offered);
  the other titles in the same list were neither accepted nor declined — do not infer.
- Only what the viewer said or clearly meant. Nothing the assistant suggested is a fact
  about the viewer; a request made once ("something funny tonight") is not a preference
  unless it repeats or they say it is.
- Keep prior facts unless this session contradicts them; then keep the newer one.
- Be proportionate. One session is one data point: write "seems to prefer", "said once that",
  not "firm boundary" or "never". Only what has come up across several sessions is stated as
  a settled preference. The assistant must still do whatever the viewer asks for next time.
- Write "(nothing yet)" as the whole profile if there is genuinely nothing durable.
"""


def _render_transcript(turns: list[dict[str, Any]]) -> str:
    lines = []
    for t in turns:
        lines.append(f"Viewer: {t.get('user', '').strip()}")
        if t.get("assistant", "").strip():
            lines.append(f"Assistant: {t['assistant'].strip()}")
    text = "\n".join(lines)
    return text[-MAX_TRANSCRIPT_CHARS:] if len(text) > MAX_TRANSCRIPT_CHARS else text


class SummaryMemoryLane(BaseMemoryLane):
    def __init__(self, settings: Settings, *, client: Any | None = None) -> None:
        super().__init__()
        self._root = Path(settings.memory_root)
        self._model = settings.memory_model or settings.llm_model
        self._max_words = settings.memory_profile_max_words
        self._extra_body = settings.llm_extra_body or None
        self._client = client or AsyncOpenAI(api_key=settings.nebius_api_key, base_url=settings.nebius_base_url)
        self._locks: dict[str, asyncio.Lock] = {}
        self._profiles: dict[str, tuple[float, MemoryBlock]] = {}  # user -> (mtime, block)

    # --- paths ---------------------------------------------------------------

    def _dir(self, user_id: str) -> Path:
        return self._root / user_id

    def _lock(self, user_id: str) -> asyncio.Lock:
        return self._locks.setdefault(user_id, asyncio.Lock())

    def read_profile(self, user_id: str) -> str:
        path = self._dir(user_id) / PROFILE_FILE
        return path.read_text().strip() if path.exists() else ""

    def pending_turns(self, user_id: str) -> list[dict[str, Any]]:
        path = self._dir(user_id) / PENDING_FILE
        if not path.exists():
            return []
        turns = []
        for line in path.read_text().splitlines():
            try:
                turns.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a torn write from a crash loses one turn, not the session
        return turns

    # --- BaseMemoryLane ----------------------------------------------------

    async def _search(self, user_id: str, text: str) -> MemoryBlock:
        path = self._dir(user_id) / PROFILE_FILE
        mtime = path.stat().st_mtime if path.exists() else 0.0
        cached = self._profiles.get(user_id)
        if cached and cached[0] == mtime:
            return cached[1]
        profile = await asyncio.to_thread(self.read_profile, user_id)
        block = MemoryBlock(profile=profile, token_est=len(profile) // 4)
        self._profiles[user_id] = (mtime, block)
        return block

    async def _ingest(self, user_id: str, user_text: str, assistant_text: str) -> dict:
        record = {"ts": time.time(), "user": user_text, "assistant": assistant_text}

        def append() -> None:
            d = self._dir(user_id)
            d.mkdir(parents=True, exist_ok=True)
            with (d / PENDING_FILE).open("a") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

        await asyncio.to_thread(append)
        return {"facts": [], "memory_ids": [], "pending": True}

    async def finish_session(self, user_id: str) -> None:
        """Fold the pending transcript into the profile. Safe to call with nothing pending."""
        async with self._lock(user_id):
            turns = self.pending_turns(user_id)
            if not turns:
                return
            log = logger.bind(user_id=user_id)
            t0 = time.perf_counter()
            previous = self.read_profile(user_id)
            try:
                profile = await self._summarise(previous, turns)
            except Exception as err:  # noqa: BLE001 — the transcript stays pending for the next attempt
                log.opt(exception=err).warning("memory summary failed; transcript kept for retry",
                                               turns=len(turns))
                return
            await asyncio.to_thread(self._commit, user_id, profile)
            log.info("memory profile updated", turns=len(turns), chars=len(profile),
                     ms=round((time.perf_counter() - t0) * 1000))

    async def warmup(self) -> None:
        """Catch-up: sessions that ended without a summary (crash, shutdown)."""
        if not self._root.exists():
            return
        users = [p.name for p in self._root.iterdir() if (p / PENDING_FILE).exists()]
        for user_id in users:
            await self.finish_session(user_id)
        if users:
            logger.info("memory catch-up done", users=len(users))

    # --- internals -------------------------------------------------------------

    async def _summarise(self, previous: str, turns: list[dict[str, Any]]) -> str:
        user_content = (
            f"# Previous profile\n{previous or '(nothing yet)'}\n\n"
            f"# Session transcript\n{_render_transcript(turns)}\n\n"
            "Now write the new profile."
        )
        resp = await self._client.chat.completions.create(
            model=self._model, temperature=0.1, max_tokens=self._max_words * 3,
            messages=[{"role": "system", "content": SUMMARY_SYSTEM.format(max_words=self._max_words)},
                      {"role": "user", "content": user_content}],
            extra_body=self._extra_body,
        )
        text = (resp.choices[0].message.content or "").strip()
        if not text:
            raise ValueError("empty summary")
        return text

    def _commit(self, user_id: str, profile: str) -> None:
        d = self._dir(user_id)
        (d / PROFILE_FILE).write_text(profile + "\n")
        archive = d / SESSIONS_DIR
        archive.mkdir(exist_ok=True)
        (d / PENDING_FILE).rename(archive / f"{time.strftime('%Y%m%dT%H%M%S')}.jsonl")
        self._profiles.pop(user_id, None)
