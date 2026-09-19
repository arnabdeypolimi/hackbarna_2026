# Phase 2 findings — SGR agent, VoiceMem memory lane, TMDB recommendations

**Date:** 2026-09-19
**Scope:** `docs/superpowers/plans/2026-09-19-tv-avatar-backend-phase2.md`, Tasks 1–9.
**Status:** all nine tasks implemented; unit suite **115 passed, 1 skipped** (the skip is the
live VoiceMem test, gated on `VOICEMEM_LIVE=1` — it passes when run). Text-mode end-to-end
turns measured live against Nebius (`tools/smoke_turn.py`). Spoken path (SLNG + Anam)
**not measured** — no SLNG/Anam keys in the dev `.env`.

## What was verified live

### Dependencies (Task 1)
`uv add openai polars pyarrow aiosqlite qdrant-client voicemem` resolved in-process against
the pinned Pipecat stack (`pipecat-ai 1.11.0`, `pipecat-slng 0.5.2`, `pipecat-anam 0.2.0a6`).
`voicemem 0.2.3` pulled `torch 2.14`, `transformers 4.52.3`, `sentence-transformers 5.7`,
`mem0ai`, `sherpa-onnx`, `funasr` — heavy but conflict-free. **The HTTP sidecar fallback was not
needed.**

### VoiceMem text lane (Task 5) — answers to the plan's open questions
| Question | Answer (voicemem 0.2.3, from source) |
|---|---|
| Does `VoiceMem.from_config` exist? | Yes. Used as planned. |
| Which `mode` accepts plain-text `ingest()` and still runs the rightbrain? | The **default `mode="text_mode"`** (alias `"text"`). `ingest(text=...)` sets `speaker="user"` and runs the full Ingest pipeline incl. rightbrain. |
| Does `warmup()` load audio models? | `warmup(audio=True)` does (ASR/VAD/perception). We warm **E5 directly** via `voicemem.leftbrain.local_e5_embedder.shared_e5()` in `VoiceMemLane.warmup()` — no audio models are ever loaded. |
| Where does E5 come from? | `VOICEMEM_MODELS_DIR/embedding` if present, else the HF id `intfloat/multilingual-e5-small`, auto-downloaded on first use (~30 s once, ~5 s warm load thereafter). The `hf download zhifeixie/VoiceMem_Default_Models_Env` step is **optional**. The env var is `VOICEMEM_MODELS_DIR` (our `VOICEMEM_LOCAL_MODELS_DIR` setting maps to it). |
| Does search return speaker/emotion on text input? | `SearchResult.hits` carry `attributed_to` (always `"user"` in text mode); no acoustic emotion. We render `result_leftbrain` + `result_rightbrain` + `rb_directive` into `MemoryBlock`. |

**Two hidden cloud embedding calls on the ingest path.** VoiceMem's graph-entity layer and
slot-naming use the OpenAI SDK with `OPENAI_EMBEDDING_MODEL` (default `text-embedding-3-small`)
and expect `VOICEMEM_EMBED_DIM=1536`. On Nebius that 404s. `VoiceMemLane` now sets
`OPENAI_EMBEDDING_MODEL=Qwen/Qwen3-Embedding-8B` and `VOICEMEM_EMBED_DIM=4096` (its native width;
VoiceMem does not pass `dimensions=`). Both calls are off the turn.

**`.env` is not the process environment.** VoiceMem's internal clients read
`os.environ["OPENAI_API_KEY"/"OPENAI_BASE_URL"]`; pydantic-settings does not export `.env`. The
first smoke run therefore had VoiceMem's `ConflictResolver` hitting `api.openai.com` (401). Fixed
by passing `api_key`/`base_url` in the `llm.config` block of `from_config`, which makes VoiceMem
export them itself (its documented behaviour). No `os.environ` writes in `Settings` (D7 kept).

Live measurement (`VOICEMEM_LIVE=1 uv run pytest -k slow_ingest`): E5 warm load 7.9 s cold-cache
→ ~5 s warm; `prefetch` while a 5 s cloud ingest runs returned inside budget (test bound 0.7 s,
observed ≈ 20 ms after warmup); ingest extracted `"User dislikes horror"` from
"I hate horror films".

### SGR envelope on Nebius (Tasks 6–7)
`turn_plan_schema()` — the pydantic-generated strict schema with `$defs`, nullable fields and
a 14-way `anyOf` action union — is **accepted by `nvidia/Nemotron-3_5-Lightning`** with
`response_format=json_schema, strict=true`; keys stream in declared order; every envelope
validated with `TurnPlan.model_validate_json`. `EnvelopeStreamer` produced
`IntentReady → SayDelta* → ActionReady* → Done` on every live turn. **D8 held; the native
tool-calls fallback was not needed.**

Pipecat gotchas found while implementing `SGRAgentService(LLMService)`:
- `LLMService` owns `self._settings` (an `LLMSettings`). Naming our config attribute the same
  broke `StartFrame` (`'Settings' object has no attribute 'validate_complete'`). Ours is `_cfg`.
- Pipecat logs an ERROR at start unless **every** `LLMSettings` field is initialised
  (`None` for unsupported). Done in `__init__`.
- `LLMService` emits an `LLMServiceMetadataFrame` at start; tests filter it.
- `push_frame` is asynchronous to the next processor — "filler before action" must be asserted
  at the agent's push, not at the sink.
- `run_test`'s system frames (`UserStartedSpeakingFrame`, `InterruptionFrame`) bypass the
  processor queue; tests separate them with `SleepFrame`.

### End-to-end text-mode turns (Task 9, `tools/smoke_turn.py`, 500-title catalog, `Nemotron-3_5-Lightning`)
| Turn | intent | said | commands | cycles | TTFT | first action | total |
|---|---|---|---|---|---|---|---|
| "something like Sicario, but newer" | recommend/answer | "Let me look." + "I found a few newer thrillers like Retribution (2023), John Wick 3 (2019), Kandahar (2023)…" | `focus 762430` | 2 | 348 ms | 587 ms | 1488 ms |
| "play the first one" | control | "On it." | `play 565770` (tile 0) | 1 | 412 ms | 515 ms | 520 ms |
| "what did I just start watching?" | answer | "You started Blue Beetle." (from history) | — | 1 | 316 ms | — | 361 ms |

`recall_ms=0` on every turn — the prefetch fired from the (simulated) partial was a hit each
time. Budget check against spec §5: filler at ~350 ms, first TV command at ~500 ms, single-cycle
turns complete in ~0.4–0.5 s; the recommendation turn's second cycle adds ~1 s (a second full
LLM completion) but the user hears "Let me look." at 350 ms.

**Bugs found and fixed by the live run** (the unit suite did not catch them):
1. `RecsEngine` gave the query-embed hop the *whole* tool budget, so on an embed timeout the
   taste/popular fallback never ran and `recommend_titles` returned `unavailable`. The embed hop
   now gets 60 % of the budget.
2. `needs_second_cycle` skipped cycle 2 on a failed tool, leaving the user with only the filler.
   Any internal tool call now earns the second cycle so the fallback is spoken.
3. The OpenAI stream was not closed on early `Done`, leaking an `aclose()` error at shutdown.
4. Prompt: questions produced spurious `focus`/`resume` actions; added an explicit
   "questions → intent answer, empty actions" rule. Held on re-run.

### Recommendation engine (Tasks 2, 4)
- Catalog build: 500 titles, Nebius `Qwen3-Embedding-8B` @ 1024 dims, 2 batches, ~9 s incl. the
  parquet pass over the 630 MB CSV. Resumable (second run: `embedded=0 skipped=500`).
- Local Qdrant ignores payload indexes (warning) — filters still work; removed the index calls.
- Query embedding jitter is real: the 400 ms outer budget was hit repeatedly at 240 ms embed
  sub-budget; the fallback path (taste/popular + `"for you"` reasons) is what the user hears then.
  `MemoryPrefetchTap` also calls `RecsEngine.prefetch_query`, so in the real pipeline the vector
  is usually cached before the turn opens.

### Control-plane seam (Task 8)
`CommandBus.push_server_message` + queue widened to `ServerMessage`; `cancel_turn` filters on
`isinstance(m, CommandMsg)` (tested). `AgentStatusObserver` derives
listening/thinking/speaking/idle from frames and pushes user/assistant `TranscriptMsg` captions.

**Phase-1 gap closed:** nothing exposed the media plane over HTTP. Added
`POST /sessions/{id}/offer` (SmallWebRTC offer/answer) and a **Talk** button in the mock client
so M0/M1 can now actually be run.

## Not done / next
- **M0/M1 spoken path** (SLNG STT partials → prefetch → SGR → SLNG TTS → Anam; barge-in stop
  time): blocked on `SLNG_API_KEY`, `ANAM_API_KEY`, `ANAM_AVATAR_ID`. The `TurnLatencyObserver`
  line (`turn latency | ttft_ms=… interrupt_stop_ms=…`) is the data source once keys exist.
- Bench re-run at n=10 (`tools/bench_llm.py`) — not repeated; live turns show TTFT 316–412 ms,
  consistent with the n=2 table.
- Full 100 k catalog build (`uv run python tools/build_catalog.py`) — only the 500-title slice
  was built.
- Nemotron labels `intent` loosely (a recommendation turn came back `answer`); harmless because
  routing is by actions, not by intent.

## Live chat-mode run, second pass (same `user_id` across two `smoke_turn.py` sessions)

Session 1: "I really can't stand horror films, and I love slow-burn crime thrillers" → ingested
`"User strongly dislikes horror…"`. Session 2 (new process, same `user_id=couch_live`):

| Turn | said | commands | total |
|---|---|---|---|
| "recommend me something for tonight" | "Let me look." + "…Retribution (2023), Fast X (2023), Kandahar (2023). I'll focus on Retribution since it's a mystery thriller and you enjoy slow-burn crime thrillers." | `focus 762430` | 1576 ms |
| "play the first one" | "On it." | `play 565770` (tile [0]) | 472 ms |
| "what kind of films do I hate?" | "You strongly dislike horror films and love slow-burn crime thrillers." | — | 477 ms |

Memory → recs warm start works (the `say` cites the stored preference); recall across sessions
works from VoiceMem alone (no history row involved).

Fixed on this pass: (5) `RecsEngine` embedded the query and the memory text *sequentially*, each
with the full embed sub-budget → two timeouts blew the 400 ms tool budget; now `asyncio.gather`.
(6) Nemotron emits `genre="crime thriller"`; TMDB genres are atomic (`Crime`, `Thriller`), so the
hard filter matched nothing → `parse_genres()` maps free text to ANY-of TMDB genres.
(7) Prompt: "play X" occasionally became `focus`; rule tightened.
