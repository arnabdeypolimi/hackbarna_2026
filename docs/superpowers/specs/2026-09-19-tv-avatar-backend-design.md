# TV Avatar Backend — Design Spec

**Date:** 2026-09-19
**Status:** Draft for review
**Scope:** Backend only. The TV frontend is out of scope except for the wire protocol it must speak and the mock client bundled for development.

---

## 1. Goal

A conversational avatar that sits on a transparent overlay above a live TV UI. As the user talks to it, it moves the content grid, plays a title on "yes", controls playback, and surfaces shoppable products tied to what is on screen.

The avatar must feel seamless: interruptible mid-sentence, and fast enough that navigation reads as reflex rather than as a request being processed.

### Non-goals

- The TV frontend itself — it is a **separately deployed application** with its own repository. This backend's obligation to it is the wire protocol in §7 and the generated type artifacts. A mock client is bundled for development and demo.
- The content catalog and product data sources. The backend consumes them through a single `search_catalog` tool and through screen state pushed by the client.
- User accounts, billing, and multi-tenant concerns.

---

## 2. Locked decisions

These were settled during design and everything below follows from them.

| # | Decision | Rationale |
|---|---|---|
| D1 | **Two planes: WebRTC for media, a separate WebSocket for control** | Cleanest separation. The UI command contract is independent of the call lifecycle, so a teammate can build the TV app against a stable protocol, and a control-socket reconnect does not drop the call. |
| D2 | **Client pushes screen state** | The TV app stays the source of truth about its own UI. Deixis ("that one", "the second") resolves with zero latency at turn time, because the snapshot is already in memory when the user speaks. The alternative — the backend owning the navigation model — means two state machines that drift. |
| D3 | **Prose streams to TTS; UI actions ride typed tool calls** | A single JSON envelope per turn forces the avatar to wait for full generation before saying a word. Tool calls give native Pipecat streaming *and* let commands fire mid-sentence. Structure is preserved: every command is a validated Pydantic schema. |
| D4 | **LLM provider is pluggable** | The agent boundary is defined in terms of Pipecat's LLM service interface, so any provider drops in without touching the tool layer or the prompt assembly. |
| D5 | **`SmallWebRTCTransport`, not Daily** | No third-party account or room provisioning for a hackathon-speed build. Daily remains a drop-in swap later if TURN reliability on real TV hardware demands it. |

### Rejected alternatives

- **Single JSON envelope (`{"speech": ..., "commands": [...]}`)** — adds full generation latency as dead air before the first word. Rejected on D3.
- **Speech-first streaming JSON with a partial parser** — solves the latency but requires writing and debugging incremental JSON parsing under time pressure, with ugly recovery when output is malformed mid-stream. Rejected on D3.
- **Backend owns UI state, client is a dumb renderer** — perfect agent knowledge, but duplicates the TV app's state machine on the server. Rejected on D2.
- **Tool-call-on-demand for screen state (`get_screen_state()`)** — cleaner prompt, but adds an LLM round-trip mid-turn, which is directly felt on a voice interface. Rejected on D2; retained only for `search_catalog`, where the data genuinely is not on screen.
- **`AnamTransport` direct Daily egress** — documented as experimental and cara-4 only, with signalling that may break between Anam alpha releases. Rejected on D5.

---

## 3. Architecture

Two planes, deliberately independent. They meet only at the session store and the command bus.

```mermaid
flowchart LR
    subgraph TV["TV App (client)"]
        MIC[Microphone]
        VID[Avatar overlay]
        UI[Content grid / player]
    end

    subgraph BE["Backend"]
        direction TB
        API[FastAPI<br/>session lifecycle]
        subgraph MEDIA["Media plane — Pipecat pipeline"]
            direction LR
            IN[transport.input] --> STT[SlngSTTService]
            STT --> UAGG[user aggregator]
            UAGG --> INJ[ScreenContextInjector]
            INJ --> LLM[TVAgentLLM<br/>+ tool handlers]
            LLM --> TTS[SlngTTSService]
            TTS --> ANAM[AnamVideoService]
            ANAM --> OUT[transport.output]
        end
        subgraph CTRL["Control plane"]
            WS[ControlChannel<br/>WebSocket]
            BUS[(Command bus<br/>asyncio.Queue)]
            STATE[(SessionState<br/>ScreenState snapshot)]
        end
    end

    subgraph EXT["External"]
        SLNG[SLNG<br/>STT + TTS]
        ANAMC[Anam Cloud]
        LLMP[LLM provider]
    end

    MIC -->|WebRTC audio| IN
    OUT -->|WebRTC A/V| VID
    STT <-.->|wss| SLNG
    TTS <-.->|wss| SLNG
    ANAM <-.->|SDK| ANAMC
    LLM <-.->|https| LLMP

    LLM -->|validated commands| BUS
    BUS --> WS
    WS -->|command| UI
    UI -->|screen_state| WS
    WS --> STATE
    STATE --> INJ

    TV -->|POST /sessions| API
    API -.->|spawns| MEDIA
```

**Why `ScreenContextInjector` sits between the aggregator and the LLM:** screen state must be as fresh as the moment the user stopped speaking, not as fresh as session start. Injecting it as a frame processor gives every LLM run the latest snapshot without polluting conversation history with stale screens.

---

## 4. The pipeline

```python
Pipeline([
    transport.input(),          # SmallWebRTC — mic in, A/V out
    stt,                        # SlngSTTService, partials on
    user_agg,                   # LLMContextAggregatorPair.user()
    screen_injector,            # custom: stamps current ScreenState
    llm,                        # custom: pluggable service + tool handlers
    tts,                        # SlngTTSService
    anam,                       # AnamVideoService (audio passthrough)
    transport.output(),
    assistant_agg,
])
```

Only two boxes are ours: `screen_injector` and the tool layer around `llm`. Everything else is library code wired from config.

### Service configuration

| Service | Key settings |
|---|---|
| `SlngSTTService` | `model="slng/deepgram/nova:3-en"`, `enable_partials=True`, `enable_vad=True`, `base_url` set to the nearest regional hub (`eu.api.slng.ai`) |
| `SlngTTSService` | `model="slng/deepgram/aura:2-en"`, `voice` pinned in config, same regional `base_url` |
| `AnamVideoService` | `persona_config` with `enable_audio_passthrough=True` (our TTS drives the avatar, not Anam's voice), `enable_session_replay=False`, `avatar_model="cara-4"`, `video_width=768`, `video_height=1152` |

Voice, speed, and language are **pinned for the session**: changing any of them mid-session forces a WebSocket reconnect to redo SLNG's init handshake, which would be heard as a gap.

### VAD placement

Run `SileroVADAnalyzer` **locally in the transport** rather than relying solely on SLNG's server-side VAD. Barge-in detection is the one thing that must not pay a network round-trip — the avatar has to stop the instant the user starts. SLNG's server VAD stays enabled for transcript segmentation, where an extra ~100 ms is invisible.

### Media contract

**Video is continuous for the life of the session.** Once the Anam session connects, `AnamVideoService` forwards decoded audio and video frames for as long as the session is open. When the avatar is not speaking it is still rendering idle motion, so the WebRTC video track never goes silent and the avatar does not pop in and out between turns. Consequences:

- Anam session time and WebRTC bandwidth are a **standing cost** for as long as the overlay is up, not a per-utterance cost.
- Hiding the avatar when idle is a **frontend** decision — fade the overlay, keep the stream. Tearing down the Anam session would cost seconds of reconnect latency on the next utterance.
- Anam's idle-loop behavior is inferred from how persistent avatar sessions work, not stated in `pipecat-anam`'s docs. **M1 confirms it by observation.**

**The microphone is always hot.** `transport.input()` feeds audio continuously regardless of whether the avatar is speaking; this is what makes barge-in (§9) possible at any moment.

**Every hop streams**, which is the only reason the §5 latency budget holds — no stage waits for the previous one to complete.

| Hop | Streaming | Granularity |
|---|---|---|
| Mic → STT | Yes (WebSocket) | Continuous audio in; partials + finals out |
| STT → LLM | **Gated by turn** | One LLM run per endpoint, not per partial |
| LLM → TTS | Yes | Sentence/clause chunks, not tokens |
| LLM → tool dispatch | Yes | Fires per complete, schema-valid call |
| TTS → Anam | Yes (WebSocket) | Audio frames as synthesized |
| Anam → client | Yes (WebRTC) | Continuous synced A/V |

Two qualifications on "everything streams":

1. **STT → LLM batches by turn.** Transcripts arrive as partials, but the aggregator holds them until VAD endpointing reports the user has finished, then fires one LLM run. Running per-partial would multiply cost and answer half-finished sentences. The 300–500 ms endpointing delay is the largest single line in the latency budget; speculative execution on partials is the only place worth optimizing if it must shrink.
2. **LLM → TTS streams at clause granularity.** Pipecat aggregates tokens to sentence or clause boundaries before synthesis, because word-by-word TTS produces choppy prosody. The avatar begins sentence one while the model writes sentence two — not one token per audio chunk.

Tool calls are dispatched only once **complete and schema-valid** — a partially-streamed `play(title_id=` is never sent. Once valid, dispatch is immediate and does not wait for the rest of the turn, which is what produces the parallelism in §5.

### Frame rate

**Target: 25 fps** (PAL-native, matching European broadcast).

The frame rate is set by Anam's renderer; the pipeline forwards what it decodes. `pipecat-anam`'s docs specify resolution but not output fps, so **M1 measures delivered fps** rather than assuming it. If the source rate exceeds the display rate, match the display to the source — naive frame-dropping produces visible judder on smooth head motion.

**The server performs no per-frame pixel work.** At 768×1152 packed RGB24, 25 fps is ~66 MB/s passing through Python, on the same asyncio event loop as STT, TTS, and LLM streaming. Ten milliseconds of per-frame processing consumes 25% of the loop and surfaces not as dropped video but as **audio stutter and late interruption handling** — the video path and conversational responsiveness compete for the same loop. The video path is therefore a pure passthrough: decode, forward, never touch pixels.

Aspect correction, if needed, is handled by (a) requesting a matching resolution from Anam, or (b) the client's compositing pass, which exists anyway for the masked overlay and runs on the GPU for free.

25 fps is a target, not a guarantee: WebRTC adapts resolution and frame rate under congestion. Instrument **actual delivered fps** in M4 alongside latency metrics.

---

## 5. Turn flow

```mermaid
sequenceDiagram
    autonumber
    participant U as User
    participant TV as TV App
    participant P as Pipeline
    participant S as SLNG
    participant L as LLM
    participant A as Anam
    participant W as Control WS

    TV->>W: screen_state {rail, tiles, focus, playback}
    Note over W: stored in SessionState

    U->>P: "play the second one"
    P->>S: audio stream
    S-->>P: transcript + endpoint
    P->>P: inject ScreenState into context
    P->>L: run (prose + tools)

    par UI acts immediately
        L-->>P: tool_call play(title_id="tt_88")
        P->>P: validate against schema
        P->>W: command {verb:"play", id:"cmd_7"}
        W->>TV: command
        TV->>TV: start playback
        TV-->>W: ack {id:"cmd_7", ok:true}
    and Avatar speaks
        L-->>P: "Sure, putting that on."
        P->>S: TTS stream
        S-->>P: audio
        P->>A: audio (passthrough)
        A-->>P: synced audio + video
        P->>TV: WebRTC A/V
    end

    TV->>W: screen_state {playback: playing}
```

The parallel block is the payoff from D3: the grid moves while the sentence is still being spoken.

### Latency budget (target, first avatar word)

| Stage | Target |
|---|---|
| Mic → STT partial | 100–200 ms |
| Endpointing (VAD silence) | 300–500 ms |
| LLM time-to-first-token | 300–600 ms |
| TTS time-to-first-byte | 100–200 ms |
| Anam render + network | 200–400 ms |
| **Total to first word** | **~1.0–1.7 s** |
| **Total to first UI command** | **~0.5–0.8 s** (fires at tool-call time, ahead of speech) |

---

## 6. Session lifecycle

```mermaid
stateDiagram-v2
    [*] --> Provisioning: POST /sessions
    Provisioning --> AwaitingMedia: session_id + WebRTC offer + control URL
    AwaitingMedia --> Active: WebRTC connected AND control WS connected
    Active --> Degraded: control WS dropped
    Degraded --> Active: control WS reconnects (same session_id)
    Active --> Closing: client hangs up / idle timeout
    Degraded --> Closing: reconnect window expires
    Closing --> [*]: pipeline cancelled, Anam session closed
```

`Degraded` is a real state, not an error: media keeps flowing and the avatar can still converse, but commands queue rather than dispatch. This is the direct benefit of D1 — a control-socket blip does not kill the call.

---

## 7. Control protocol

Both directions use discriminated unions on a `type` field, modelled with Pydantic so validation is symmetric.

### Client → server

| Type | Payload | Notes |
|---|---|---|
| `screen_state` | `rail_id`, `tiles[] {title_id, name, position}`, `focus_index`, `playback {state, title_id, position_s}`, `view` | Sent on every UI change. Latest wins; no history kept. |
| `ack` | `command_id`, `ok`, `error?` | Confirms a dispatched command. Used for logging and `search_catalog` correlation. |
| `result` | `command_id`, `data` | Response payload for request-response commands. |
| `user_event` | `event`, `detail` | The user acted via the physical remote. Keeps the agent aware of changes it did not cause. |

### Server → client

| Type | Payload | Notes |
|---|---|---|
| `command` | `id`, `verb`, `args`, `turn_id`, `ts` | A validated UI action. |
| `agent_status` | `state` ∈ `idle`/`listening`/`thinking`/`speaking` | Drives overlay affordances (e.g. a listening indicator). |
| `transcript` | `role`, `text`, `final` | Optional captions. |
| `error` | `code`, `message` | Protocol or upstream failures. |

### Command envelope

```json
{
  "v": 1,
  "type": "command",
  "id": "cmd_7",
  "turn_id": "turn_3",
  "verb": "play",
  "args": { "title_id": "tt_88" },
  "ts": 1758278400.123
}
```

`turn_id` is what makes turn-scoped cancellation possible (§9).

### Protocol versioning

The TV frontend is a **separately deployed application** (§1), so the two codebases ship on independent schedules. Every message in both directions carries `"v"`. The backend accepts the current version and rejects unknown ones with an `error` message rather than failing silently on a missing field. Without this, a frontend built against today's argument shape misbehaves invisibly when the backend adds a required one.

### Contract distribution

`agent/commands.py` is the single source of truth for command shapes (§8), but that value only exists inside Python. A build step emits **JSON Schema and TypeScript type definitions** from the Pydantic models as a committed artifact the frontend repository consumes. This extends the no-drift property that the capability manifest gives the prompt: the TV app cannot compile a command the backend would reject. Without generated types the two repositories drift, and the drift is discovered at integration time.

### Control channel authentication

Within a single application `session_id` was effectively private. Across two applications it travels in a URL, so it is not a credential. `POST /sessions` mints a **short-lived control token** bound to the session; the WebSocket rejects connections presenting no token or a token for a different session. This also brings CORS configuration and `wss://` termination into scope — concerns a single-application design would not have.

---

## 8. The agent

### Structured output — command vocabulary

A closed verb set, each one a Pydantic model. Unknown verbs are rejected by the validation layer before reaching the bus.

| Verb | Args | Kind |
|---|---|---|
| `play` | `title_id`, `resume_from?` | fire-and-forget |
| `pause` | — | fire-and-forget |
| `resume` | — | fire-and-forget |
| `seek` | exactly one of `to_seconds` or `delta_seconds` | fire-and-forget |
| `navigate` | `direction` ∈ `up`/`down`/`left`/`right`, `count` | fire-and-forget |
| `focus` | `title_id` | fire-and-forget |
| `open_details` | `title_id` | fire-and-forget |
| `close` | — | fire-and-forget |
| `back` | — | fire-and-forget |
| `home` | — | fire-and-forget |
| `show_products` | `title_id`, `scene_at?` | fire-and-forget |
| `search_catalog` | `query`, `limit` | **request-response** |

**Dispatch semantics.** Fire-and-forget handlers enqueue and return `{"status": "dispatched"}` immediately, without awaiting the client's ack — a slow TV must never stall the LLM turn, because stalling the turn stalls speech. `search_catalog` is the sole exception: it awaits a correlation-id future with a hard **400 ms** timeout, degrading to a spoken "I couldn't reach the catalog just now" rather than hanging the conversation.

### Structured prompt

Assembled per turn from five fixed sections:

1. **Role and persona** — who the avatar is, tone, brevity rules.
2. **Capability manifest** — the available verbs and their arguments.
3. **Current screen state** — a compact rendering of the latest snapshot.
4. **Interaction rules** — act first then narrate; keep replies to one or two sentences; never invent a `title_id` that is not in screen state or a search result; ask before destructive actions.
5. **Output contract** — speak in plain prose, express every UI action as a tool call.

**The capability manifest is generated from the same Pydantic models that define the tools.** Prompt and schema therefore cannot drift — which removes the most common failure mode in this class of system, a prompt advertising a verb the validator rejects.

Sections 1, 2, 4, and 5 are static across a session and go first; section 3 is volatile and goes last, so a stable prefix can be cached if the chosen provider supports it.

---

## 9. Interruption semantics

Barge-in is not merely "stop the audio". Four rules, one of them deliberately a non-action:

1. **Stop speech.** Pipecat's `StartInterruptionFrame` cancels TTS and LLM downstream.
2. **Flush the avatar.** `AnamVideoService` buffers early TTS audio by design so nothing is dropped during startup. That buffer must be dropped on interruption, or the avatar keeps talking after the user has cut in. **This is the highest-risk unknown in the design** and is verified in M1, before anything is built on top of it.
3. **Cancel unsent commands.** The command bus is turn-scoped: queued-but-unsent commands carrying the interrupted `turn_id` are dropped.
4. **Do not roll back dispatched commands.** If `play` already reached the TV, it stays played. Silently undoing a visible action is worse than surprising the user, who can simply say "no, go back".

---

## 10. Repository structure

```
src/tv_avatar/
  config.py               # pydantic-settings: keys, models, voices, regions
  app.py                  # FastAPI: POST /sessions, WS /sessions/{id}/control
  session/
    manager.py            # lifecycle, per-session task supervision
    state.py              # ScreenState, SessionState
  control/
    protocol.py           # wire models — discriminated unions both directions
    channel.py            # WS read/write loops, correlation futures
  agent/
    commands.py           # Pydantic command schemas  <- single source of truth
    tools.py              # schemas -> tool defs + handlers
    prompt.py             # structured prompt assembly
    injector.py           # ScreenContextInjector (FrameProcessor)
    llm.py                # pluggable LLM service construction
  pipeline/
    builder.py            # build_pipeline(transport, session) -> PipelineTask
    services.py           # SLNG + Anam construction from config
    observers.py          # latency metrics, agent_status emission
tests/
tools/
  mock_tv_client/         # browser page: video + fake grid, speaks the protocol
  export_schemas.py       # Pydantic models -> JSON Schema + TypeScript types
contracts/                # generated artifacts, committed, consumed by the TV repo
```

Each unit has one purpose and a defined interface: `commands.py` owns the schemas and nothing else imports Pipecat; `channel.py` knows the wire format but not the agent; `builder.py` knows Pipecat but not the protocol. `tools/mock_tv_client/` is not optional scaffolding — it is how the protocol gets exercised before the real TV frontend exists, and how the system is demoed if the frontend slips.

### Configuration and secrets

All configuration is read from the environment through `pydantic-settings` in `config.py`; nothing is hardcoded and no key appears in source. Local development supplies them via `.env`, which is git-ignored; `.env.example` is committed with every key present and every value blank.

| Variable | Purpose |
|---|---|
| `SLNG_API_KEY` | SLNG gateway — covers both STT and TTS |
| `SLNG_BASE_URL` | Regional hub, e.g. `eu.api.slng.ai` (not a secret; defaulted in config) |
| `ANAM_API_KEY` | Anam session authentication |
| `ANAM_AVATAR_ID` | Persona selection |
| *(LLM provider key)* | Named once the provider is chosen (§14) |
| `SLNG_PROVIDER_KEY` | Optional — BYOK, external routes only |

Startup fails fast on a missing required key rather than surfacing it as a connection error mid-session.

---

## 11. Build order

| # | Milestone | Proves |
|---|---|---|
| M0 | Voice loop, no avatar: WebRTC → SLNG STT → LLM → SLNG TTS → out | The SLNG plugin works on the pinned Pipecat version |
| M1 | Add `AnamVideoService` + **interruption test** | The riskiest unknown, early |
| M2 | Control WS + protocol + mock TV client | A teammate can build against a real contract |
| M3 | Screen state injection + tools + structured prompt | The agent understands "that one" |
| M4 | Turn-scoped cancellation + latency instrumentation | It feels seamless, measurably |
| M5 | Shoppable products, persona / director notes, polish | Demo quality |

---

## 12. Testing strategy

| Layer | Approach |
|---|---|
| Command schemas | Pure unit tests — valid args accepted, unknown verbs and out-of-range args rejected |
| Prompt assembly | Snapshot tests over a fixed `ScreenState`; a test asserts the manifest matches the schema registry, catching drift |
| Control protocol | Round-trip serialization tests both directions |
| Command bus | Async tests for turn-scoped cancellation and for the `search_catalog` timeout path |
| Pipeline | Integration test with fake STT/LLM/TTS services, asserting frame ordering and interruption behavior |
| End-to-end | Manual via the mock TV client; the interruption case is a scripted manual check |

---

## 13. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| **Prerelease trap.** `pipecat-anam`'s *stable* release (0.1.0) requires `pipecat-ai>=0.0.103` — the legacy Pipecat line. The modern package is the `0.2.0a6` **prerelease**, which requires `pipecat-ai>=1.8.0`. `uv` does not select prereleases by default, so a plain `uv add pipecat-anam` silently installs the legacy version against the wrong Pipecat. | Medium | Pin `pipecat-anam==0.2.0a6` explicitly with prereleases allowed for that package only. Verified at install time by an import-and-version assertion test (Task 1). |
| **Version skew** between the SLNG and Anam plugins. Resolved as of 2026-09-19: `pipecat-slng` 0.5.2 and `pipecat-anam` 0.2.0a6 both require `pipecat-ai>=1.8.0` and Python ≥3.11. The SLNG *documentation* still says "tested against v1.3.0"; the published package has moved past it. | Low | M0 still proves the combination end to end, but the dependency floors agree. Re-check on any dependency bump. |
| **Anam interruption / buffer behavior** on barge-in is undocumented. | High | Verified in M1 as an explicit test, not as a side effect. |
| **Sample-rate alignment** between SLNG TTS output and what Anam expects. | Medium | Set the pipeline sample rate explicitly and pass it to both services rather than relying on defaults. |
| **Overlay transparency.** WebRTC carries no alpha channel; Anam sends opaque RGB. | Medium | The "transparent overlay" is a client-side masked composite of an opaque portrait feed (cara-4, 768×1152). Documented as a frontend requirement, not a backend one. |
| **Tool-call chatter** — a weaker model narrating without calling a tool, or inventing `title_id`s. | Medium | Explicit interaction rules in the prompt; validation layer rejects ids absent from screen state or recent search results. |
| **Aspect-ratio mismatch** between the Anam feed and the overlay slot. | Low | Request a matching resolution from Anam, or correct it in the client's compositing pass. **Do not** adopt `pipecat-anam`'s `CenterAspectCropFilter` example — per-frame pixel work on the event loop is ruled out by the frame-rate budget in §4. |
| **Event-loop contention** between the 25 fps video path and conversational responsiveness. | Medium | No per-frame pixel work server-side (§4). Measure delivered fps and interruption latency together in M4 — regressions show up in audio before they show up in video. |
| **Credentials arrive late.** M0 and M1 — the two highest-risk milestones — both require live keys to validate. | Medium | Build against interfaces meanwhile, but treat key provisioning as the critical path: unretired risk stays unretired until they land. |

---

## 14. Deferred: the agent layer

The agent — provider choice, prompt wording, tool-calling behavior, and the reasoning quality bar — is **deliberately deferred** and will get its own design pass. Everything in §8 stands as the *interface* the agent must satisfy; how it satisfies it is open.

Implementation therefore proceeds in two phases:

- **Phase 1 (this spec's plan): media and control planes, agent stubbed.** `agent/llm.py` ships a placeholder service that satisfies the Pipecat LLM interface — deterministic canned responses and scripted tool calls — so the pipeline, protocol, interruption semantics, and mock client are all fully testable without a provider or a key. A stub is also *better* for this phase: deterministic output makes frame-ordering and interruption tests reproducible in a way a real model never could.
- **Phase 2 (separate spec + plan): the real agent.** Provider selection, prompt assembly, tool handlers, screen-state injection tuning.

The stub's value outlasts phase 1 — it stays as the test double for the pipeline's integration tests (§12).

Also deferred, and not affecting any interface above: the **product data source** for `show_products` — whether products arrive in the client's screen state or are fetched server-side (M5).
