# Codebase review — performance, robustness, simplicity

Date: 2026-09-20. Scope: `src/tv_avatar/` (all modules), `frontend/src/` (App, hooks, lib,
PosterRow), plus the `fix/avatar-auto-reconnect` branch. Read-only review; nothing below has
been changed. Baseline: `uv run pytest` 220 passed; `uvx ruff check src tests tools` 12
findings (8 auto-fixable, all cosmetic — import order, an unused import, two `C401`s).

Verdict up front: the architecture is sound and the hot path (audio → STT → LLM first byte)
is already engineered with measured budgets. The real findings are (a) redundant SQLite work
on the turn's critical path, (b) three unbounded in-memory structures, (c) two caches that
never invalidate, and (d) some duplication that can be collapsed. Nothing is a stage-killer,
but P1 items 1–3 are worth doing before the next long demo.

Severity: **P1** fix soon · **P2** worth doing · **P3** nice-to-have.

---

## 1. Backend — turn latency (critical path)

### P1 · History is rendered twice per turn, six SQLite round-trips instead of three
`ScreenContextInjector._stamp` calls `history.render_for_prompt()` and stamps it into the
system message. Then `SGRAgentService._turn` (service.py:204) calls
`history.render_for_prompt()` **again** and `build_messages` (service.py:429–438) discards
the injector's copy when it sees `# Screen`. Each render is `recent_titles` +
`recent_recommended` + `rejected_ids` = 3 queries, so a turn does 6 awaited queries before
the LLM request is sent. On the dev box this is a few ms; on a slow disk or a large
`events` table it is directly added to TTFT.

Fix: have the injector stamp only `# Screen` (it already has the catalog), and let the agent
own `# Memory` + `# Recent activity` — or the reverse: the injector renders history into
`SessionState` (e.g. `session.history_text`) and the agent reads it. Either removes one of
the two call sites and the `"# Screen" in existing["content"]` string sniff in
`build_messages`, which is the fragile part.

### P2 · `HistoryStore` reads are full per-user scans with Python-side dedupe
`recent_titles`, `rejected_ids`, `recent_recommended`, `engaged_ids` all `SELECT … ORDER BY
ts DESC` with no `LIMIT`, then dedupe in Python (`if title_id in out` — O(n²) on a list).
`rec_shown` writes one row per recommended title per call, so it is the fastest-growing
kind. The index is `(user_id, kind)` with no `ts`, so every query sorts.

Fix: add `ts` to the index (`(user_id, kind, ts DESC)`), let SQLite dedupe
(`GROUP BY title_id` + `MAX(ts)`), and `LIMIT`. `rejected_ids`' forgive-on-play rule can be
one query: `rec_rejected` rows whose `ts` is greater than the user's latest `play_started`
for that title.

### P2 · `on_rec_shown` commits once per title
`HistoryRecorder.on_rec_shown` loops `store.record()` → N `INSERT` + N `COMMIT`. Use
`executemany` and a single commit. Off the turn (spawned), but it competes for the one
aiosqlite connection with the on-turn reads above.

### P2 · `turn_plan_schema()` and `build_system_prompt()` are rebuilt on every request
`turn_plan_schema()` does `TurnPlan.model_json_schema()` + `deepcopy` + `_strictify` per
LLM call (service.py:326), and `build_system_prompt()` re-runs `describe_capabilities()`
(field introspection over every model) on each turn via `build_messages` and the injector.
Both are pure functions of module constants. `@functools.cache` on `turn_plan_schema` and
on `build_system_prompt(language)` (LanguageProfile is frozen, hashable) — sub-millisecond
each, but it is free and removes a per-turn allocation spike.

### P3 · `for cycle in (1,):` in `_turn` is a loop that never loops
service.py:221–245 — a `for` over a one-tuple with `break` on every path. It reads as a
loop that was later capped. Replace with straight-line code: run cycle 1; if
`needs_second_cycle`, build feedback and run `_cycle_with_budget`. Same behaviour, fewer
branches to reason about, and `marks["cycles"]` becomes obvious.

### P3 · `_schedule_ingest` can be invoked twice per interruption
`_cancel_turn` and `_run_turn`'s `except CancelledError` both call it; the second call is a
no-op only because the first cleared `_turn_user_text`. It works, but the invariant is
implicit. Either make `_cancel_turn` the single owner (and have `_run_turn` only re-raise)
or add the guard explicitly.

---

## 2. Backend — unbounded growth and stale caches

### P1 · `CommandBus._outbound` is unbounded while the TV is disconnected
`SessionEventsObserver` publishes an `agent_status` or `transcript` message for **every**
`InterimTranscriptionFrame`, bot start/stop, etc. In the Degraded state (control socket
down — exactly the scenario the reconnect PR addresses) `_write_loop` is not draining, so a
voice session with no TV attached accumulates every partial transcript for up to the 1 h TTL.
Tens of thousands of Pydantic objects per hour is plausible.

Fix: the spec only requires *commands* to survive a disconnect. Give the deque a `maxlen`
for non-command messages, or drop `agent_status`/`transcript` when there is no consumer
(`next_outbound` not awaited). Simplest: two deques — commands (unbounded, turn-scoped) and
events (`deque(maxlen=64)`), with `next_outbound` preferring commands.

### P1 · `RecsEngine._taste_cache` is never invalidated
`invalidate_taste` exists but has no callers (grep: only the definition). The taste vector
is computed once per user per process and then frozen — a viewer who finishes three films
tonight gets the same "for you" channel until the server restarts. Either call
`invalidate_taste` from `HistoryRecorder` on `PLAY_COMPLETED`/`PLAY_ABANDONED`, or give the
cache a TTL (a few minutes is fine — taste is slow-moving).

### P2 · `RecsEngine._query_cache` grows forever
Entries are checked against `QUERY_CACHE_TTL_S` only on read and never evicted. Every
utterance ≥ 6 chars adds a `(user, 20-char prefix) → 384 floats` entry via
`prefetch_query`. Small per entry, but it is a leak. Evict on insert (drop expired entries
when `len > N`) or use a bounded `OrderedDict`.

### P2 · `SummaryMemoryLane._locks` and `_profiles` are per-user and never pruned
Negligible per entry; noted for completeness. A `weakref`-free fix is to pop the lock in
`finish_session` when not contended.

### P3 · `SessionStore.others_for_user` and `sweep_expired` are linear scans
Fine at demo scale (tens of sessions). If sessions ever number in the thousands, key a
second dict by `user_id`. Not worth doing now.

---

## 3. Backend — robustness

### P2 · `ControlChannel._write_loop` pops before sending — a failed send loses the message
`next_outbound()` pops from the deque, then `send_text` awaits. If the socket dies between
the two, that message is gone, and the spec's "commands queue until the TV reconnects"
promise is broken for exactly one command per disconnect (often the one that mattered).
Fix: peek, send, then pop; or on send failure push the message back to the front.

### ~~P2 · `ControlChannel.run` cancels the loser but does not await it~~
Withdrawn: a cancelled task never logs "exception was never retrieved", and awaiting the
loser inside the handler breaks under an ASGI server that cancels the handler on
disconnect (Starlette's TestClient does). The peek-then-pop change above is what actually
protects the in-flight message.

### P2 · `_missing_settings()` re-parses `.env` on every call when unconfigured
`get_settings()` is `lru_cache`d only on success; a `ValidationError` is not cached, so an
unconfigured backend re-reads `.env` for every `/config`, `POST /sessions` and `/offer`.
Harmless for a demo, but it is called three times per session setup. Cache the failure
outcome for the process lifetime too (settings do not change without a restart).

### ~~P3 · `app.py` `offer()` stops other pipelines before checking `_settings_available()`~~
Withdrawn: `test_new_offer_for_same_user_stops_that_users_other_pipelines` pins this order on
purpose (replace first, 503 after). Not a finding.

### P3 · `e5.encode` serialises all inference behind one `threading.Lock`
Intentional (documented crash), and correct. Note only that `asyncio.to_thread` uses the
default executor, so a slow encode also occupies one of its workers; with prefetch on every
utterance this is fine, but if the executor is ever shared with blocking file I/O
(`SummaryMemoryLane` uses `to_thread` too) consider a dedicated single-thread executor for
E5 so it cannot starve the others.

---

## 4. Frontend — performance

### P2 · `parseCSV` is a char-by-char loop with string concatenation on the main thread
`csv.ts:6–27` — for a 50 MB file (`MAX_FILE_BYTES`) this is 50 M iterations of
`field += c` on the UI thread: several seconds of frozen screen on a TV CPU, and the drop
zone / toast cannot animate. Two cheap wins: (1) scan with `indexOf`/`slice` per field
instead of per character (10× fewer iterations for typical CSV), (2) run `toTitles` in a
`Worker` so the stage stays responsive. The `public/data/titles.csv` load path is small
and unaffected; this matters for the manual import.

### P2 · `buildRow` re-sorts the whole catalogue whenever `myList` changes
`row` is memoised on `[catalog, tab, query, myList, rail]`; saving a title (which only
matters for `tab === 'list'`) re-runs the popular/top/recent sort of up to 2000 items.
Split the memo: sort per tab once (`useMemo` on `[catalog, tab]`), then filter by query /
myList on top. Also `myList.includes` inside `filter` is O(n·m) — build a `Set` first.

### P3 · `PosterRow` does `saved.indexOf(it.id)` twice per poster per render
30 posters × 2 lookups × list length. Trivial today; pass a `Set<string>` (or a
`saved: boolean` on the item) and it disappears.

### P3 · `fromWireId` is a linear `catalog.find` per id
`show_titles` with 12 ids = 12 × 2000 comparisons. Fine. If it grows, a `Map` keyed on both
`id` and `id:${id}` built alongside `catalog` in `useMemo` makes it O(1).

### P3 · `move()` queries every `.f` in scope and calls `getBoundingClientRect` on each
Per key press: `querySelectorAll('.f')` + `getBoundingClientRect` for every focusable, plus
`isVisible` (another rect). On a TV this is dozens of layout reads per press. Works today
because the DOM is deliberately small (`ROW_MAX = 30`). Leave it, but do not raise
`ROW_MAX` without revisiting.

---

## 5. Frontend — simplification

### P2 · `App.tsx` (602 lines) repeats the "close overlay, restore focus" pattern four times
`closeProfiles`, `closeThemes`, `closeDialog`, `closePlayer` all do:
```ts
setXOpen(false);
const back = prevFocus.current;
requestAnimationFrame(() => (back && document.contains(back) ? back.focus() : focusRow()));
```
and `openProfiles`/`openThemes`/`openDialog`/`openTrailer`/`watch` all do
`prevFocus.current = document.activeElement`. One `useOverlay()` hook returning
`{ open, close, scopeRef }` with the focus bookkeeping inside removes ~40 lines and four
places to get it subtly wrong. `move()`'s scope chain (`profilesOpen ? … : themeOpen ? …`)
then becomes "the topmost open overlay's scope".

### P3 · `tv.current = {...}` builds 13 closures every render
Intentional and documented (App.tsx:80). It is the right trade-off for correctness; noting
only that if App re-renders become frequent (e.g. playback position ticks at 4 Hz), this is
allocation you could avoid with `useEvent`-style stable callbacks. Not worth doing now.

### P3 · `watch()` records history and sends `user_event` before checking `trailerKey`
A title with no trailer is recorded as watched and reported to the backend as `watch`, then
the toast says "No trailer available". The backend's `HistoryRecorder` only turns
`screen_state` playback transitions into `PLAY_STARTED`, so the backend log is right, but
the local `history:<id>` gets a false entry. Move the `trailerKey` guard first.

---

## 6. The reconnect branch (`fix/avatar-auto-reconnect`)

Reviewed the diff against `origin/dev`. It is correct and follows the file's existing
generation-fence idiom; three notes:

- **P3 · `everLive` is never reset.** After one live session, *any* later failure — including
  a language switch that the backend rejects with 422 — shows "Reconnecting to X…" and
  retries twice before showing the real message. Acceptable, but a 4xx from `POST /sessions`
  is not a blip; skip the retry when `e.message` matches `/failed \(4\d\d\)/` or, cleaner,
  have `createSession` throw a typed error the hook can test.
- **P3 · `ws.onerror` + `ws.onclose` both call `onError`.** The second is fenced by
  `mineStill()` because `fail()` bumped `gen`. Correct, but it relies on `fail()` always
  bumping; a comment on `avatarClient.ts:206–210` that the hook expects and tolerates the
  double call would save the next reader a minute.
- The user-switch-during-backoff behaviour (pending reconnect proceeds and picks up the
  new `userId`) is right but implicit — worth one line in the `opts.userId` effect comment.

---

## 7. What is already good (do not "fix")

- Every network hop on the turn has a budget and a degraded path (`RECALL_BUDGET_S`,
  `EMBED_BUDGET_FRACTION`, `cycle2_first_byte_s`, `render_fallback`). This is the hard part
  and it is done.
- `EnvelopeStreamer` is a correct incremental JSON scanner that never raises; char-by-char
  Python is fine at ~300 chars/turn.
- `CommandBus` turn-scoping and the `search_catalog` single await are exactly per spec.
- `SessionEventsObserver` stays out of the media path (invariant held).
- `iceGathered`'s 2 s cap, `keepalive: true` on the DELETE, and the `parts.closing` fence
  in `avatarClient.ts` are all the right calls, with the reasons written down.
- The frontend's spatial navigation deliberately trades per-press layout reads for a tiny
  DOM; that is the correct trade on a TV.

---

## 8. Suggested order

1. Bus bounding (§2 P1) — cheap, prevents a memory leak in the exact scenario the reconnect
   PR makes common.
2. Single history render per turn (§1 P1) — removes 3 awaited queries from TTFT and the
   `"# Screen"` string sniff.
3. Taste cache invalidation (§2 P1) — one call site in `HistoryRecorder`.
4. History query shape + index (§1 P2) — one migration line, bounded reads.
5. Write-loop peek-then-pop (§3 P2).
6. `App.tsx` overlay hook (§5 P2) and `buildRow` memo split (§4 P2).
7. `ruff --fix` for the 8 mechanical findings; hand-fix `B008` (`Body(default_factory=…)`
   → module-level `_EMPTY = CreateSessionRequest()`), `TRY004`, and the two `C401`s.
