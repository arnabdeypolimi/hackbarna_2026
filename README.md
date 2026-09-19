# tv-avatar

A conversational avatar for a television. It sits on a transparent overlay above a live
TV UI, listens through the microphone, answers in a synthesised voice with a talking-head
video, and drives the UI — moving the grid, opening details, starting playback — while it
talks. It is interruptible mid-sentence.

This repository holds the **backend** (`src/tv_avatar/`) and the **TV frontend**
(`frontend/`). They are separate applications that meet only at the wire protocol in
`src/tv_avatar/control/protocol.py` and the generated artifacts in `contracts/` — the
frontend does not import the backend, and the backend serves none of its assets.

---

## Architecture

Two planes, deliberately independent. They meet only at the session store and the command bus.

```
  TV app / browser                     Backend                          External
  ────────────────                     ───────                          ────────
  microphone  ──── WebRTC audio ──▶  transport.input
                                          │
                                     SlngSTTService  ◀────── wss ─────▶  SLNG (Deepgram Nova 3 STT)
                                          │
                                     user aggregator  (VAD + silence timer)
                                          │
                                     LLM service      ◀────── https ───▶  Nebius Token Factory
                                          │
                                     SlngTTSService   ◀────── wss ─────▶  SLNG (Cartesia Sonic 3)
                                          │
                                     AnamVideoService ◀────── SDK ─────▶  Anam Cloud
                                          │
  avatar video ◀─── WebRTC A/V ───  transport.output

  TV UI       ◀─── WebSocket ────▶  ControlChannel ──▶ CommandBus
                (control plane)          │
                                    SessionState (latest screen snapshot)
```

- **Media plane** — one Pipecat pipeline per session over `SmallWebRTCTransport`.
- **Control plane** — a separate authenticated WebSocket. Commands go down, screen state
  comes up. A dropped control socket does not drop the call; commands queue until the TV
  app reconnects with the same session id.
- **Screen state is pushed by the client.** The TV app stays the source of truth about its
  own UI, so "that one" and "the second" resolve with no extra round-trip.

The full design, including the rejected alternatives, is in
[`docs/superpowers/specs/2026-09-19-tv-avatar-backend-design.md`](docs/superpowers/specs/2026-09-19-tv-avatar-backend-design.md).

---

## Quickstart

Requires Python 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                      # installs runtime + dev dependencies from uv.lock
cp .env.example .env         # then fill in the four keys (see below)
uv run uvicorn tv_avatar.app:app --reload --port 8000
```

Open <http://localhost:8000/> — it redirects to the engineering console at `/demo/`.
Press **Connect**, allow the microphone, and the avatar greets you first. The console shows
live WebRTC stats, per-turn timings (user stop → first word → done), the transcript, and a
raw wire log of every control-plane message.

`/mock/` serves a second page: a fake content grid that speaks the control protocol and
reacts to commands. Use it to exercise the protocol without the real TV app.

Without a `.env` the server still starts, `/config` reports which keys are missing, and the
console says so in its header — but `POST /sessions/{id}/offer` returns **503**, because
media cannot be produced without the providers.

### Keys

| Variable | Where to get it |
|---|---|
| `NEBIUS_API_KEY` | <https://tokenfactory.nebius.com/> — LLM, OpenAI-compatible API |
| `SLNG_API_KEY` | SLNG gateway — one key covers both STT and TTS |
| `ANAM_API_KEY` | Anam Cloud — avatar session authentication |

Every other setting has a working default in `config.py`. Nothing is hardcoded and no key
appears in source; `.env` is git-ignored and `.env.example` is committed with blank values.

### Avatars and languages

Avatar ids, their Anam model and their Cartesia voice are not env vars: they live in
`avatars.yaml` at the repo root (override the path with `AVATARS_FILE`). The file also lists
the supported session languages — English, Spanish, French, Catalan — and, per avatar, which
of them it may speak. A session pins one avatar and one language when it is created:

```json
POST /sessions  {"avatar": "lucia", "language": "es"}
```

Both fields are optional; an empty body yields the catalog defaults, and omitting the language
for a single-language avatar picks its own. `GET /config` lists the options (without provider
ids) so a client can build pickers. Adding an avatar is a YAML edit — no code changes.

Cartesia Sonic has no Catalan, so the `ca` entry carries `tts_language: es`: speech
recognition and the prompt run in Catalan, the synthesiser voices the Catalan text with its
Spanish model. The `.yaml` is validated at boot; a broken file stops the server the same way a
missing key does.

---

## HTTP and WebSocket surface

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/config` | Non-secret view of the configured stack; lists missing env vars |
| `POST` | `/sessions` | Mint a session pinned to an avatar + language; returns token, control and offer URLs |
| `POST` | `/sessions/{id}/offer?token=` | WebRTC offer → answer. Spawns the session's pipeline |
| `PATCH` | `/sessions/{id}/offer?token=` | Trickle ICE candidate |
| `DELETE` | `/sessions/{id}` | Explicit hang-up; token in the `X-Control-Token` header |
| `WS` | `/sessions/{id}/control?token=` | Control channel |

The offer endpoint takes two query flags: `avatar=false` runs the voice loop with no
talking head, and `halfduplex=true` mutes the microphone while the avatar speaks. Half-duplex
disables barge-in, so it is a diagnostic aid for laptop-speaker setups where the avatar's own
voice re-enters the microphone — not the product behaviour.

The session id travels in URLs across application boundaries, so it is **not** a credential.
The control token is. An unknown session and a bad token are reported identically, so neither
leaks the existence of the other.

### Protocol

Every message carries `"v": 1`. An unknown version is an explicit error, never a silently
missing field.

- **Client → server:** `screen_state`, `ack`, `result`, `user_event`
- **Server → client:** `command`, `agent_status`, `transcript`, `error`

Twelve command verbs are defined in `src/tv_avatar/agent/commands.py`: `play`, `pause`,
`resume`, `seek`, `navigate`, `focus`, `open_details`, `close`, `back`, `home`,
`show_products`, `search_catalog`.

Commands are **fire-and-forget** — the backend never waits for the TV app, because a slow
client would stall the LLM turn and stall speech with it. `search_catalog` is the single
exception: it blocks the turn for at most 400 ms and then answers `{"status": "unavailable"}`.

Commands are also **turn-scoped**. When the viewer barges in, commands the interrupted turn
had queued but not yet sent are dropped. Commands already on the wire are never rolled back.

### Generated contracts

`contracts/protocol.schema.json` and `contracts/protocol.d.ts` are committed artifacts,
regenerated from the Pydantic models:

```bash
uv run python tools/export_schemas.py
```

The TV app cannot compile a command this backend would reject, because both sides derive
from `agent/commands.py`. Never hand-edit the files in `contracts/`.

---

## Layout

```
src/tv_avatar/
  config.py               pydantic-settings: keys, models, voices, regions
  app.py                  FastAPI: sessions, WebRTC signalling, control WS, static mounts
  agent/
    commands.py           command schemas — the single source of truth for the vocabulary
    envelope.py           the SGR turn envelope: intent / say / actions, plus internal tools
    service.py            SGRAgentService — streams `say` sentence by sentence, dispatches actions
    stream_parse.py       incremental parser for the streamed envelope
    tools.py              internal tools: recommend_titles, recall_memory, reject_title
    injector.py           stamps screen / history sections into the system prompt per turn
    prompt.py             system prompts, written for the ear rather than the screen
    llm.py                provider construction + the deterministic scripted stub
  memory/
    lane.py               MemoryLane protocol (prefetch / recall / ingest / finish_session)
    summary_lane.py       one LLM-written profile per viewer, rewritten when a session ends
  recs/
    catalog.py            TMDB catalog: parquet rows + embedded Qdrant index
    engine.py             retrieve -> filter -> re-rank; query / taste / popular channels
    embedder.py           local E5 (default) or Nebius embeddings for queries
  history/
    store.py              per-viewer viewing log (SQLite): played, shown, rejected
    recorder.py           screen transitions and tool results -> history events
  e5.py                   the process-shared multilingual-e5-small
  runtime.py              process-wide catalog / history / memory / recs, warmed at boot
  control/
    protocol.py           wire models, discriminated unions in both directions
    channel.py            WebSocket read/write loops
    bus.py                turn-scoped command queue and search correlation
  pipeline/
    builder.py            build_pipeline(...) -> PipelineTask
    transport.py          SmallWebRTC transport parameters
    services.py           SLNG STT/TTS and Anam construction
    turns.py              VAD and turn-taking configuration
    observers.py          frames -> agent_status / transcript events
    runner.py             run one session's pipeline to completion
  session/
    state.py              SessionState, ScreenState snapshot, in-memory SessionStore
    manager.py            per-session command bus and pipeline task supervision
tools/
  demo/                   engineering console (WebRTC + control plane, live metrics)
  mock_tv_client/         fake content grid that speaks the control protocol
  export_schemas.py       Pydantic models -> JSON Schema + TypeScript
  build_catalog.py        TMDB CSV -> data/catalog.parquet + data/qdrant_db (run once)
  smoke_turn.py           text-mode end-to-end smoke of the agent, memory and recs
  voice_smoke.py          voice-API smoke harness (STT/TTS round trip, no browser)
contracts/                generated, committed, consumed by the frontend
docs/                     design spec, implementation plans, measured findings
frontend/                 Titan Browse — the React TV UI (own README, own toolchain)
```

Each unit has one purpose and a defined interface: `commands.py` imports no Pipecat,
`channel.py` knows the wire format but not the agent, `builder.py` knows Pipecat but not
the protocol.

---

## Tests

```bash
uv run pytest                    # ~10 s, no network and no API keys needed
```

Command schemas and the protocol are covered by unit and round-trip tests; the command bus
has async tests for turn-scoped cancellation and the `search_catalog` timeout; the pipeline
is exercised with the scripted stub LLM, asserting frame ordering and interruption
behaviour. End-to-end is manual, through the console at `/demo/`.

---

## Measured decisions

These came out of measurement on 2026-09-19, not from defaults:

- **LLM: `Qwen/Qwen3-30B-A3B-Instruct-2507` on Nebius**, 0.40 s median time to first token.
  Reasoning models (DeepSeek-V4-Flash, GLM-5.x, Kimi) think for ~1 s before the first word,
  which is heard as dead air on a voice interface.
- **Turn-taking is a plain silence timer** (`TURN_SILENCE_S`, default 0.5 s) on top of
  Silero VAD, not the smart-turn model. A timer is predictable and cannot hold a turn open
  on a "sounds unfinished" verdict. Speaker echo trips VAD without producing words, so a
  turn with VAD activity and no transcript is abandoned after 2 s.
- **SLNG regional routing is a header**, `X-World-Part-Override`. Per-region hostnames such
  as `eu.api.slng.ai` do not resolve.

More in [`docs/findings/`](docs/findings/).

---

## Status

Milestones M0–M3 of the design spec are done: the voice loop, the avatar with its
interruption behaviour, the control protocol with a mock client, and the agent layer.

### Agent layer (phase 2)

`AGENT_IMPL=sgr` (default) runs `SGRAgentService`: a Schema-Guided-Reasoning agent whose
every turn is one JSON envelope — `intent`, `say`, `actions[]` — produced with constrained
decoding. `say` streams to TTS sentence by sentence while actions dispatch in parallel;
internal tools (`recommend_titles`, `recall_memory`) earn one bounded second cycle to speak
their results, with a templated fallback if the model is late.

- **Recommendations** — a TMDB slice indexed in embedded Qdrant with the local
  `multilingual-e5-small` (~15 ms per query). Build it once:

  ```bash
  uv run python tools/build_catalog.py --limit 500      # data/ is git-ignored
  ```

- **Viewing log** — `data/history.db`: what was played, what the agent offered, what the
  viewer declined (`reject_title`). The greeting and the recommender take titles from here.
- **Memory** — one profile per viewer in `data/memory/<user_id>/profile.md`, rewritten by
  the LLM from the session transcript when the session ends (crash-safe: leftovers are
  folded in at the next start). Durable preferences and tone, readable and editable by hand.
- **Viewer identity** — `POST /sessions {"user_id": "..."}`; sessions come and go, the
  couch persists.

Text-mode smoke, no speech keys needed:

```bash
uv run python tools/smoke_turn.py --user couch_1 "something like Sicario" "no, not the first one"
```

Not built yet: **M4** latency instrumentation beyond the console and the per-turn log line;
**M5** shoppable products and persona polish. The session store is an in-memory dict —
swapping in Redis touches `SessionStore` and nothing else.

### The frontend

`frontend/` is Titan Browse: a 10-foot browse screen for Titan OS TVs, driven entirely by a
remote control. React 18 + TypeScript + Vite, two runtime dependencies, targeting Chrome 84
on 1–1.5 GB TV boards. It has its own README, its own toolchain and its own dataset:

```bash
cd frontend && npm install && npm run dev   # http://localhost:5173
```

It is **not yet wired to this backend.** Watch, Episodes and Continue record local history
and report what they would do; the YouTube trailer player is the only real playback surface.
Connecting those seams to the control protocol above is the work that joins the two halves.
