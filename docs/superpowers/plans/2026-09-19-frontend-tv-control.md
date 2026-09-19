# Frontend TV Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the agent drive the TV. Commands arriving on the control socket move focus, play, pause, seek, search and go home; the TV reports what is on screen before every turn so "the second one" and "that one" resolve; the session is keyed by the viewer's profile so history and memory persist.

**Architecture:** The framework-free session client (`avatarClient.ts`) gains a `send()` for the client→server half of the protocol it already reads. A new pure module (`tvBridge.ts`) owns the two translations that cross the wire — title ids and screen state — so `App.tsx` never sees protocol shapes. `App` derives a `ScreenState` from state it already holds and pushes it debounced; commands come back through a handler ref (the pattern `keyHandler` already uses) so the socket callback always sees the latest closures without reconnecting. `TrailerPlayer` grows an imperative handle for pause/resume/seek and reports position upward. No backend change is needed for Tasks 1–6; Task 7 is the one optional backend extension.

**Tech Stack:** React 18, TypeScript 5.8 (strict), Vite 7, WebSocket. No new dependencies.

**Spec:** the wire contract is `contracts/protocol.d.ts` (generated; never hand-edited). Backend semantics: `docs/superpowers/specs/2026-09-19-tv-avatar-backend-design.md` §4 (screen state), §8 (commands), D9 (stamp, never store), D11 (`title_id` is `str(tmdb_id)`). Reference TV implementation: `tools/mock_tv_client/client.js`.

## Global Constraints

- **Work only inside `frontend/`** for Tasks 1–6. Task 7 touches `src/tv_avatar/agent/commands.py`, `agent/envelope.py`, regenerates `contracts/` via `uv run python tools/export_schemas.py`, and adds a test. Never hand-edit `contracts/`.
- **Never run `uv` inside `frontend/`, never run `npm` outside it.**
- **Chrome 84 is the floor.** No `replaceChildren`, `Array.prototype.at`, `structuredClone`, `String.replaceAll`. Optional chaining and `??` are fine.
- **Performance budget:** screen state is pushed on change, debounced, and never per-frame. Playback position is reported by `TrailerPlayer`'s existing 1 s ticker — do not add a second timer.
- **Commands are fire-and-forget on the backend** (CLAUDE.md invariant). Only `search_catalog` awaits a `result`, for at most 400 ms. Every other verb gets an `ack`. Nothing here may block; every handler branch is synchronous or resolves within one render.
- **Every outbound message carries `v: 1`**, set in exactly one place (`send()`).
- **Turn scoping stays.** The existing `mineStill()` guard in `useAvatar` must wrap the command path so a superseded session's late command never reaches the current screen.
- **Ids cross the wire bare.** The backend's `title_id` is `str(tmdb_id)`; the app's `Title.id` is `id:<tmdb>` or `Name (year)`. Translation happens only in `tvBridge.ts`.
- **Style:** 2-space indent, single quotes, semicolons. Comments explain *why*, at the codebase's existing density.
- **Commits:** Conventional Commits. GitHub, `gh`. Branch: `feat/frontend-avatar-panel`, continued — it is already rebased onto the `dev` that carries the SGR agent, and this work is the panel's natural second half.
- **Build gate:** `npm run build` (`tsc --noEmit && vite build`). No frontend test runner exists; every task ends with a build check *and* a named manual observation against a running backend (`uv run uvicorn tv_avatar.app:app --reload --port 8000`, `.env` with provider keys). Turn on `console.debug` filtering for `[avatar]` in DevTools.
- **Backend catalog:** run `uv run python tools/build_catalog.py` once so `recommend_titles` has data and `ScreenContextInjector` can enrich tiles. Without it the agent still works but recommendations degrade to "popular".

---

## File Structure

**Created**

| File | Responsibility |
| --- | --- |
| `frontend/src/lib/tvBridge.ts` | Pure: `toWireId`, `fromWireId`, `deriveScreenState`, `searchCatalog`. No React, no sockets. |
| `frontend/src/hooks/useTvControl.ts` | React: debounced `screen_state` push, and the `command` → handler dispatch with `ack`/`result`. |

**Modified**

| File | Change |
| --- | --- |
| `frontend/src/lib/avatarClient.ts` | `AvatarSession.send(msg: ClientMessage)`; `user_id` in `POST /sessions`. |
| `frontend/src/hooks/useAvatar.ts` | Accept `userId` and a command-handler ref; expose `send`. Drop the `TODO(M3)`. |
| `frontend/src/components/TrailerPlayer.tsx` | `forwardRef` + `useImperativeHandle` `{pause, resume, seekTo, seekBy}`; `onPlayback` callback. |
| `frontend/src/App.tsx` | Build the `CommandHandler`, hold the player ref and playback state, derive and push `ScreenState`, emit `user_event`s. |
| `frontend/README.md` | Document what the agent can now do and how ids are mapped. |

---

### Task 1: Outbound path — `send()` and `user_id`

The client reads all four server message types but can send none of the four client ones. This task gives it a `send()` and identifies the viewer.

**Files:**
- Modify: `frontend/src/lib/avatarClient.ts`
- Modify: `frontend/src/hooks/useAvatar.ts`

**Interfaces:**
- Consumes: `ClientMessage` from `@contracts/protocol`.
- Produces: `AvatarSession.send(msg: Omit<ClientMessage, 'v'>): void`; `ConnectOptions.userId?: string`.

- [ ] **Step 1: Add `send` to `AvatarSession`**

In `avatarClient.ts`, extend the interface and implement it in `connect()` next to `close`:

```ts
export interface AvatarSession {
  // ...existing fields...
  /** Client→server half of the protocol. A no-op on a socket that is not open. */
  send(msg: Omit<ClientMessage, 'v'>): void;
}
```

```ts
const send = (msg: Omit<ClientMessage, 'v'>) => {
  const ws = parts.ws;
  // Dropping is correct: screen state is latest-wins and re-sent on change, and a
  // result for a command the backend has already timed out is of no use to it.
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ v: 1, ...msg }));
};
```

`v: 1` is stamped here and nowhere else.

- [ ] **Step 2: Send the viewer's id**

Add `userId?: string` to `ConnectOptions`; `createSession(avatar, language, userId)` posts `{ avatar, language, user_id: userId }`. The backend keys history and memory by it (`SessionStore.create`, D10); without it every session is `anon_<session_id>` and nothing persists.

- [ ] **Step 3: Thread through `useAvatar`**

`useAvatar(video, { userId })`. Pass `userId` to `connect()`. Store the live session so a later hook can call `session.current?.send(...)`; expose `send` on `AvatarView` as a stable callback that forwards to the current session (or no-ops).

- [ ] **Step 4: Verify**

`npm run build` passes. Start a session; in the backend log the `POST /sessions` line is followed by a turn whose log binding reads `user_id=<profile id>` rather than `anon_…`.

**Commit:** `feat(frontend): client→server messages and a viewer id on the session`

---

### Task 2: The bridge — ids and screen state

Everything that knows both `Title` and the wire protocol lives here, and it is pure so it can be reasoned about (and later tested) without React.

**Files:**
- Create: `frontend/src/lib/tvBridge.ts`

**Interfaces:**
- Consumes: `Title`, `Tab` from `types/title`; `ScreenState`, `Tile`, `Playback` from `@contracts/protocol`.
- Produces: `toWireId`, `fromWireId`, `deriveScreenState`, `searchCatalog`, `PlaybackReport`.

- [ ] **Step 1: Id translation**

```ts
// The backend's title_id is str(tmdb_id) end to end (D11) and its catalog enriches
// tiles by that key. The app prefixes numeric dataset ids with `id:` so they cannot
// be mistaken for a title (csv.ts). Strip it on the way out, accept both on the way in.
export const toWireId = (t: Title): string => (t.id.startsWith('id:') ? t.id.slice(3) : t.id);

export function fromWireId(id: string, catalog: Title[]): Title | undefined {
  return catalog.find((t) => t.id === `id:${id}` || t.id === id);
}
```

- [ ] **Step 2: Screen state derivation**

```ts
export interface PlaybackReport { state: Playback['state']; position_s: number }

// The prompt lists every tile it is given, so send a window, not the row: the model
// needs the neighbours of the focus to resolve "the next one", not all 500 titles.
const WINDOW = 8;

export function deriveScreenState(args: {
  tab: Tab; query: string; row: Title[]; selIdx: number;
  playing: Title | null; playback: PlaybackReport;
}): ScreenState {
  const { tab, query, row, selIdx, playing, playback } = args;
  const lo = Math.max(0, selIdx - WINDOW);
  const tiles: Tile[] = row.slice(lo, selIdx + WINDOW + 1)
    .map((t, i) => ({ title_id: toWireId(t), name: t.title, position: lo + i }));
  return {
    view: playing ? 'player' : 'grid',
    rail_id: query ? `search:${query}` : tab,
    focus_index: row.length ? selIdx : null,
    tiles,
    playback: playing
      ? { state: playback.state, title_id: toWireId(playing), position_s: playback.position_s }
      : { state: 'stopped', title_id: null, position_s: 0 },
  };
}
```

`position` is the absolute row index so `focus_index` and the prompt's `[n]` labels agree. The `Detail` side panel is part of `grid` — it shows whatever is focused, so it is not a separate view.

- [ ] **Step 3: Local search**

```ts
// The same matching the search bar uses (rows.ts), so a spoken and a typed search
// agree on what they find.
export function searchCatalog(catalog: Title[], query: string, limit: number): Title[] {
  const q = query.toLowerCase();
  return catalog
    .filter((x) => x.title.toLowerCase().includes(q) || x.genres.some((g) => g.toLowerCase().includes(q)))
    .sort((a, b) => b.pop - a.pop)
    .slice(0, limit);
}
```

If `rows.ts` exports its matcher, reuse it instead of duplicating the predicate.

- [ ] **Step 4: Verify**

`npm run build` passes (unused exports are fine for now — `noUnusedLocals` does not fire on exports).

**Commit:** `feat(frontend): tv bridge — id mapping, screen-state derivation, local search`

---

### Task 3: Command dispatch with `ack` and `result`

Turns the `console.debug` into action. The handler is supplied by `App` through a ref so it always closes over the latest state.

**Files:**
- Create: `frontend/src/hooks/useTvControl.ts`
- Modify: `frontend/src/hooks/useAvatar.ts`
- Modify: `frontend/src/App.tsx`

**Interfaces:**
- Consumes: `CommandMsg`, `CommandArgsByVerb` from `@contracts/protocol`; `AvatarView.send`.
- Produces:

```ts
/** What the TV does for each verb. Return a string to ack ok:false with that reason. */
export interface CommandHandler {
  play(args: CommandArgsByVerb['play']): string | void;
  pause(): string | void;
  resume(): string | void;
  seek(args: CommandArgsByVerb['seek']): string | void;
  navigate(args: CommandArgsByVerb['navigate']): string | void;
  focus(args: CommandArgsByVerb['focus']): string | void;
  open_details(args: CommandArgsByVerb['open_details']): string | void;
  close(): string | void;
  back(): string | void;
  home(): string | void;
  show_products(): string | void;
  search_catalog(args: CommandArgsByVerb['search_catalog']): Array<{ title_id: string; name: string }>;
}
```

- [ ] **Step 1: The dispatcher**

In `useTvControl.ts`, export `dispatchCommand(cmd: CommandMsg, handler: CommandHandler, send: AvatarView['send'])`:

```ts
if (cmd.verb === 'search_catalog') {
  // The one verb the backend awaits (400 ms). Pure in-memory filtering, so the
  // budget is not in play, but it must be a `result`, never an `ack`.
  const titles = handler.search_catalog(cmd.args);
  send({ type: 'result', command_id: cmd.id, data: { titles } });
  return;
}
let error: string | void;
try {
  error = (handler[cmd.verb] as (a: unknown) => string | void)(cmd.args);
} catch (e) {
  error = (e as Error).message;
}
send({ type: 'ack', command_id: cmd.id, ok: !error, error: error ?? null });
console.debug('[avatar] command', cmd.verb, cmd.args, error ?? 'ok');
```

- [ ] **Step 2: Wire it into `useAvatar`**

`useAvatar(video, { userId, onCommand: RefObject<CommandHandler> })`. In `connect()`'s `onCommand`, replace the `TODO(M3)` block with:

```ts
onCommand: (m) => {
  if (!mineStill()) return;
  const h = handler.current;
  if (h) dispatchCommand(m, h, (msg) => live?.send(msg));
},
```

(`live` is assigned once `connect()` resolves; a command cannot arrive before the socket is open, and the socket is opened inside `connect()`, so by the time one does `live` is set. If the ordering in the file makes that awkward, keep the session in `session.current` and send through it.)

- [ ] **Step 3: The handler in `App`**

```ts
const tv = useRef<CommandHandler | null>(null);
tv.current = {
  play: ({ title_id }) => {
    // Resolve against the whole catalogue, not the visible row: a recommendation the
    // agent just made may not be on the rail the viewer happens to be on.
    const t = fromWireId(title_id, catalog);
    if (!t) return `unknown title ${title_id}`;
    if (!t.trailerKey) return `no trailer for ${t.title}`;
    watch(t);
  },
  pause: () => playerRef.current ? playerRef.current.pause() : 'nothing playing',
  resume: () => playerRef.current ? playerRef.current.resume() : 'nothing playing',
  seek: ({ to_seconds, delta_seconds }) => {
    const p = playerRef.current;
    if (!p) return 'nothing playing';
    if (to_seconds != null) p.seekTo(to_seconds); else if (delta_seconds != null) p.seekBy(delta_seconds);
  },
  navigate: ({ direction, count }) => {
    for (let i = 0; i < (count ?? 1); i++) move(direction, document.activeElement as HTMLElement | null);
  },
  focus: ({ title_id }) => reveal(title_id),
  open_details: ({ title_id }) => reveal(title_id),
  close: () => back(false),
  back: () => back(false),
  home: () => { setPlayer(null); selectTab('popular'); focusRow(); },
  show_products: () => 'not supported on this TV',
  search_catalog: ({ query, limit }) =>
    searchCatalog(catalog, query, limit ?? 10).map((t) => ({ title_id: toWireId(t), name: t.title })),
};
```

where `reveal(title_id)` finds the title in `catalog`, and if it is not in the current `row`, sets `query` to its exact title so the row contains it (the search rail is the app's only way to show an arbitrary title), then `selectPoster(row.indexOf(t))` on the next render via `wantRowFocus`. Return `'unknown title …'` when nothing matches. `pause`/`resume`/`seek` use the handle from Task 4; until then they return `'not yet'` — do the tasks in order and this stub never ships.

`navigate` calling `move()` `count` times is correct because `move` is synchronous DOM focus; the `sel` state update is batched but the focused element advances each iteration.

- [ ] **Step 4: Verify**

`npm run build`. With the backend running and the catalog loaded, say **"search for space"**: backend log shows `tv command | verb=search_catalog status=ok`, the agent speaks two or three real titles from your CSV. Say **"play the first one"**: the trailer opens; log shows `verb=play`. Say **"go right twice"**: focus moves two posters. Say **"show me products"**: log shows the `ack` failure reason.

**Commit:** `feat(frontend): dispatch agent commands to the TV with ack and search results`

---

### Task 4: Playback control — `TrailerPlayer` handle and position

`pause`, `resume`, `seek`, and a truthful `playback.position_s` all need the YT player, which is private to `TrailerPlayer`.

**Files:**
- Modify: `frontend/src/components/TrailerPlayer.tsx`
- Modify: `frontend/src/App.tsx`

**Interfaces:**
- Produces:

```ts
export interface TrailerPlayerHandle {
  pause(): void; resume(): void; seekTo(s: number): void; seekBy(delta: number): void;
}
// Props gains:
onPlayback?(report: PlaybackReport): void;
```

- [ ] **Step 1: Expose the handle**

Wrap the component in `forwardRef<TrailerPlayerHandle, Props>` and `useImperativeHandle(ref, () => ({ ... }), [ready])` over the existing `p.pauseVideo()`, `p.playVideo()`, `p.seekTo()` calls (lines ~81-87 already implement toggle and seek-by; factor them so the keyboard path and the handle share one function each).

- [ ] **Step 2: Report playback**

In the existing 1 s ticker (line ~70) and in the play/pause toggle, call `onPlayback?.({ state: playing ? 'playing' : 'paused', position_s: time })`. No new timer.

- [ ] **Step 3: Hold it in `App`**

`const playerRef = useRef<TrailerPlayerHandle>(null)` and `const [playback, setPlayback] = useState<PlaybackReport>({ state: 'stopped', position_s: 0 })`. Pass both to `<TrailerPlayer ref={playerRef} onPlayback={setPlayback} … />`. Reset `playback` to stopped in `closePlayer`.

- [ ] **Step 4: Verify**

Open a trailer, say **"pause"** → it pauses, **"skip ahead thirty seconds"** → it seeks, **"resume"** → it plays. Each shows a `verb=…` line in the log.

**Commit:** `feat(frontend): imperative trailer controls and playback reporting for the agent`

---

### Task 5: Screen state push

The half that makes "the second one" work. The agent reads `# Screen` at the start of every turn; this keeps it current.

**Files:**
- Modify: `frontend/src/hooks/useTvControl.ts`
- Modify: `frontend/src/App.tsx`

- [ ] **Step 1: The push hook**

```ts
export function useScreenStatePush(state: ScreenState, send: AvatarView['send'], live: boolean) {
  useEffect(() => {
    if (!live) return;
    // Trailing-edge debounce: `navigate right 5` and a scrub both burst, and the
    // backend only reads the latest at turn start, so intermediate states are waste.
    const t = setTimeout(() => send({ type: 'screen_state', state }), 120);
    return () => clearTimeout(t);
  }, [state, send, live]);
}
```

`state` must be memoised by the caller so the effect does not fire every render.

- [ ] **Step 2: Derive and push in `App`**

```ts
const screen = useMemo(
  () => deriveScreenState({ tab, query, row, selIdx, playing: player?.item ?? null, playback }),
  [tab, query, row, selIdx, player, playback],
);
useScreenStatePush(screen, avatar.send, avatar.phase === 'live');
```

- [ ] **Step 3: Send once on connect**

In `useAvatar`, when `phase` becomes `live`, the effect above already fires because `live` flips — no extra code, but verify it: the first turn after connect must not read "Screen state: unknown".

- [ ] **Step 4: Verify**

Set the backend logger to `TRACE` for `tv_avatar.agent.service` (or add a temporary `log.debug` of `render_screen`) and confirm the `# Screen` block lists `[n] Name (id=<tmdb>) <- focused` with the *bare* TMDB id — that is what makes the backend enrich it with year and genres. Say **"play that one"** without naming it: the focused title plays. Say **"what's the third one about?"**: the agent describes the title at `[2]`.

**Commit:** `feat(frontend): report screen state so the agent can resolve "that one"`

---

### Task 6: User events and README

Cheap and useful: the backend's history recorder already handles `user_event`, so manual actions become part of what the agent knows.

**Files:**
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/README.md`

- [ ] **Step 1: Emit events**

In `watch`, `toggleSave`, `switchProfile`: `avatar.send({ type: 'user_event', event: 'watch' | 'save' | 'unsave' | 'profile_switch', detail: { title_id: toWireId(item) } })` (profile switch carries `{ user_id }`). Keep it to those three; the recorder stores `detail.title_id` and ignores the rest.

- [ ] **Step 2: Profile switch restarts the session**

A different `userId` means a different backend identity. In `useAvatar`, treat a `userId` change like a language change: `start(config, lang.current)` again. The backend tears down the previous pipeline itself (`others_for_user`), so nothing more is needed.

- [ ] **Step 3: README**

Under the avatar section: what the agent can do (the verb table), that `title_id` on the wire is the bare TMDB id, that `search_catalog` runs locally against the loaded CSV, and that the backend and frontend must be loaded with the same TMDB dataset for recommendations to be showable.

- [ ] **Step 4: Verify**

`npm run build`. Switch profile: the log shows a new `POST /sessions` with the new `user_id`, and the old pipeline logs `pipeline replaced by new offer`. Save a title with the red key: the log shows `user event save`.

**Commit:** `feat(frontend): user events to the agent; restart the session on profile switch`

---

### Task 7 (optional, backend + frontend): a `show_titles` verb for recommendation rails

Today the agent can only *speak* a recommendation and `focus` one title. A rail of posters is the most visible demo feature and needs a verb that does not exist.

**Files:**
- Modify: `src/tv_avatar/agent/commands.py` — `ShowTitles(title_ids: list[str] (1..20), label: str)`
- Modify: `src/tv_avatar/agent/envelope.py` — `_VERB_DOCS[Verb.SHOW_TITLES]`
- Modify: `src/tv_avatar/agent/prompt.py` — after `recommend_titles` returns, emit `show_titles` with the ids, then `say`.
- Regenerate: `uv run python tools/export_schemas.py`
- Add: `tests/test_commands.py::test_show_titles_bounds`
- Modify: `frontend/src/App.tsx`, `frontend/src/lib/rows.ts` — a transient `Tab`-like rail `agent:<label>` built from `title_ids` via `fromWireId`, cleared by `home`/`back`.

- [ ] **Step 1:** Add the model and doc string; `uv run pytest` still green; regenerate contracts and confirm `contracts/protocol.d.ts` gains `ShowTitlesArgs`.
- [ ] **Step 2:** Frontend: `show_titles` handler sets `agentRail = { label, items }`; `buildRow` returns it when set; the `TabBar` shows the label as a temporary chip. `deriveScreenState` reports `rail_id: 'agent:<label>'`.
- [ ] **Step 3:** Verify: "what should I watch tonight?" → a labelled rail of the agent's picks appears, the first is focused, and "play the second one" plays it.

**Commit:** `feat: show_titles verb — the agent can put a rail of picks on screen` (backend and frontend may be two commits; the contracts regeneration goes with the backend one).

---

## Order and dependencies

1 → 2 → 3 → 4 → 5 → 6 are strictly sequential; each is independently shippable and leaves the app working. 7 depends on 3 and 5 and on `dev` accepting a backend change.

## Out of scope

- A frontend test runner (spec decision on the panel branch; unchanged here).
- Validating `user_id` on the backend (`^[A-Za-z0-9_-]{1,64}$`, flagged in the PR #4 review) — should land on `dev` independently before Task 1 is merged.
- Real playback: the only footage is trailers, and `play` opens the trailer as it does today.
