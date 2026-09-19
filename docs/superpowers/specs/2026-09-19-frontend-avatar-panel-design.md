# Frontend avatar panel — design

Status: approved, not yet implemented
Branch: `feat/frontend-avatar-panel`
Supersedes nothing. Depends on the backend design in
`2026-09-19-tv-avatar-backend-design.md` (referenced below as "the backend spec").

## Problem

The two halves of this repository have never met. `tools/demo/` is an engineering
console that speaks the full protocol but looks nothing like a television, and
`frontend/` is a finished 10-foot browse UI that speaks nothing at all. A viewer
cannot currently see the avatar and the catalogue on the same screen.

This design puts the avatar into the product UI: the right-hand panel of the TV
app becomes the avatar, and the viewer picks which language it speaks.

## Scope

In scope — **presence**:

- avatar video and audio rendered in the TV app's right panel;
- microphone captured from the TV app;
- agent status (idle / listening / thinking / speaking) and the latest transcript
  line shown next to the face;
- a language picker that selects both the spoken language and, implicitly, the
  avatar who speaks it;
- session lifecycle: create, connect, swap language, hang up.

Out of scope, deliberately:

- **Acting on commands.** The agent is still `OpenAILLMService` with no tool
  calls and no screen-state injection (see "Current state" in `CLAUDE.md`), so no
  `command` message is ever emitted. Building a dispatcher now would be building
  against nothing. One seam is left for M3 and nothing more.
- **Sending `screen_state`, `ack` or `result`.** Same reason: nothing consumes
  them until the agent layer lands.
- **Translating the TV app's own copy.** Language selects the avatar's speech.
  Headings, buttons and tabs stay English; an i18n pass is its own project.
- **Backend changes.** This branch touches `frontend/` and this document only.

## Decisions

| Decision | Choice | Why |
| --- | --- | --- |
| Where the avatar lives | Replaces the resume panel outright | A half-height avatar on a 548px panel is a thumbnail, not a face. The viewer is ten feet away. |
| Scope | Presence only | The backend emits no commands yet. |
| Language ↔ avatar | One picker; language selects the avatar | Every non-English avatar speaks exactly one language, so a second picker would offer only dead combinations. |
| Picker location | Inside the avatar panel | It is a property of the thing above it, and it must be disabled while a session is live. |
| Session start | Auto-connect on mount, with a gesture fallback | Chosen for the ambient feel; the fallback exists because browsers may refuse. |
| Dev topology | Vite proxy to `:8000` | Keeps one origin for HTTP, WebSocket and WebRTC signalling, and keeps the backend free of CORS. |

## Layout

`.panel.side` keeps its geometry and its character: `left: 1308px; top: 74px;
548×900`, glass fill, and the deliberate `rotateY(-7deg)` tilt whose right edge
lands on the frame. Only the contents change.

```
┌ .panel.side ─────────┐
│  ╭────────────────╮  │   video, 484×620
│  │  avatar video  │  │   object-fit: cover; object-position: top
│  ╰────────────────╯  │
│  Lucía · listening   │   name + status pill
│  "ponme algo de…"    │   latest transcript line, clamped to 2 lines
│  ──────────────────  │
│  [EN][ES][FR][CA]    │   language chips, focusable (.f)
└──────────────────────┘
```

Anam delivers a 2:3 portrait. Rendered whole at 548px wide it would be 822px
tall and leave 78px for everything else, so the video is cropped to 620px and
anchored to the top: a head-and-shoulders framing, which is the part of a
talking head worth showing at ten feet anyway.

### What the resume panel took with it

`ResumePanel.tsx` is deleted, `pickResume()` is deleted from `lib/rows.ts`, and
the `.side-*` / `.resume-*` rules leave `styles.css`.

Watch history keeps being recorded (`historyKey`, written by `watch()`), because
it costs nothing and the agent will want it. But the product loses its only
one-press resume affordance: the tab bar is Recommended / Top rated / New
releases / My List, with no Continue tab to fall back on. This is an accepted
regression, not an oversight.

## Components

All new files live under `frontend/src/`.

### `lib/avatarClient.ts`

Framework-free session client. No React, no DOM beyond the `<video>` element it
is handed. It is `tools/demo/demo.js` with the console removed:

```
connect({ language, video, onStatus, onTranscript, onCommand, onError })
  → POST /sessions {avatar, language}
  → open control WebSocket
  → getUserMedia → RTCPeerConnection → POST offer → setRemoteDescription
  → returns { sessionId, avatar, language, close() }
```

`close()` stops the tracks, closes the peer connection and the socket, and
fires `DELETE /sessions/{id}` with the `X-Control-Token` header.

Three things the demo does that this does **not**, because each is per-frame or
per-second work on a board with a weak GPU and 1–1.5 GB of shared memory:

- no `getStats()` polling,
- no `AudioContext` microphone meter (the status pill animates in CSS instead),
- no `requestVideoFrameCallback` frame counter.

Chrome 84 is the floor (see `frontend/PRODUCT.md`), so `replaceChildren`,
`Array.prototype.at` and `structuredClone` are unavailable — the demo uses the
first of those and it must not be copied across.

### `lib/avatarCatalog.ts`

Pure, and the one piece worth testing:

```ts
avatarForLanguage(config, code): string   // es→lucia, fr→chloe, ca→pau, en→default_avatar
languageOptions(config): LanguageOption[] // only languages some avatar speaks
```

The mapping is derived from `/config`, never hardcoded: each avatar publishes a
`languages` allow-list, and `avatars.yaml` is free to change under us. Where two
avatars claim one language (English has Cara and Igor today), `default_avatar`
wins.

### `hooks/useAvatar.ts`

Owns the phase machine and nothing else:

```
off → connecting → live
        ↓            ↓
      blocked      error
```

- `blocked` — `getUserMedia` or `video.play()` was refused; recoverable by a
  user gesture.
- `error` — anything else; recoverable by the same button, which retries.

Exposes `{ phase, avatar, status, lastLine, language, setLanguage, retry }`.

### `components/AvatarPanel.tsx` and `components/LanguagePicker.tsx`

The panel renders the phase. The picker renders chips carrying the `.f` class so
the existing spatial navigation in `lib/spatialNav.ts` finds them with no change
to the navigation code; chips are `disabled` while `phase === 'connecting'`.

## Wiring

### Vite proxy

```ts
server: {
  host: true,
  proxy: {
    '/config':   'http://localhost:8000',
    '/sessions': { target: 'http://localhost:8000', ws: true },
  },
}
```

`ws: true` matters: the control channel is a WebSocket under the same `/sessions`
prefix as the HTTP signalling, so one entry covers both.

### Contracts

`tsconfig.json` gains a path mapping and the contracts directory:

```json
"paths": { "@contracts/*": ["../contracts/*"] },
"include": ["src", "../contracts"]
```

Wire types are imported type-only (`import type { ServerMessage } …`), so
nothing from `contracts/` reaches the bundle and Vite needs no `fs.allow` entry.
`verbatimModuleSyntax` is on, so the `type` keyword is mandatory.

Two limits of the generated file, both load-bearing:

1. `PROTOCOL_VERSION` is `export const` inside a `.d.ts` — a declaration with no
   runtime value. Importing it as a value fails at run time. The version is read
   from the session response's `protocol_version` instead.
2. `/config` and `POST /sessions` shapes are **not** generated. They are ad-hoc
   dicts in `app.py`, not Pydantic models, so `tools/export_schemas.py` never
   sees them. Those two interfaces are hand-written in `avatarClient.ts` with a
   comment pointing here. Promoting them to Pydantic models and generating them
   is a worthwhile follow-up, and is explicitly not done on this branch.

## Connect flow

```
mount
  └─ GET /config
       ├─ configured: false → phase "error", "Avatar unavailable — missing keys"
       └─ ok
            └─ getUserMedia
                 ├─ granted → POST /sessions → WS → offer/answer → phase "live"
                 └─ refused → phase "blocked", focusable
                              "Press OK to talk to <name>"
```

Auto-connect is the chosen behaviour, and the fallback is not optional
decoration: a browser may refuse the microphone or refuse to autoplay audio, and
either refusal must produce a focusable button rather than a dead panel.

Failure states each say what happened, because a silent grey rectangle is
indistinguishable from a broken build:

| Condition | Panel shows |
| --- | --- |
| `/config` reports `configured: false` | "Avatar unavailable" + the missing env var names |
| `/config` unreachable | "Backend not running" |
| mic or autoplay refused | "Press OK to talk to \<name\>" (focusable, retries) |
| WebRTC or socket failure | the error, plus the same retry button |

### Changing language

The backend pins avatar and language at `POST /sessions` and cannot re-pin them:
a different language means a different Cartesia voice and a different Anam
persona. So `setLanguage` hangs up and connects again. The chips are disabled
across the swap and the pill reads "Switching…". The choice persists to
`localStorage` under one global key — it is a property of the television, not of
a profile.

### Hang-up

On unmount and on `beforeunload`. `DELETE /sessions/{id}` is fire-and-forget;
the backend's sweeper reaps anything the browser fails to close.

## Commands

`onCommand` logs the message and returns. One comment marks the seam:

```ts
// TODO(M3): dispatch verbs into App state. The agent emits no commands until
// tool calls are wired (CLAUDE.md, "Current state"); see contracts/protocol.d.ts.
```

No `ack`, `result`, `screen_state` or `user_event` is sent. `search_catalog` is
the only verb that blocks an LLM turn, and it cannot arrive yet.

## Verification

`frontend/` has no test runner. `npm run build` is `tsc --noEmit && vite build`,
and that plus manual verification is the gate for this branch:

1. `npm run build` passes.
2. With `uv run uvicorn tv_avatar.app:app --port 8000` and `npm run dev`: the
   panel connects on load, the face appears, speech is audible, the status pill
   tracks the four states, and the transcript line updates.
3. Switching language reconnects and the new avatar answers in that language.
4. With the backend stopped, the panel reads "Backend not running" and the rest
   of the TV app still browses.
5. With the backend up but `.env` empty, the panel reads "Avatar unavailable".
6. Remote-only navigation reaches the language chips and the retry button.

No test framework is added on this branch. `avatarForLanguage` is the one pure
function worth a unit test, and introducing vitest for it is a separate decision.

## Risks

- **Cost.** Auto-connect opens a paid Anam and Cartesia session on every page
  load, including every hot reload during development. Anyone leaving `npm run
  dev` open is spending money.
- **The real television.** `getUserMedia` requires a secure context. Served to a
  TV over plain http on the LAN it is blocked outright, and no in-page fallback
  can rescue it — that needs https or a Chrome origin flag. Desktop development
  on `localhost` is unaffected. This is the one thing standing between this
  branch and a demo on actual hardware.
- **Deployment.** The Vite proxy is a development-server feature. Shipping the
  frontend and backend to separate origins would need the CORS route instead.
