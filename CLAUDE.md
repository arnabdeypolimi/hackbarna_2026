# CLAUDE.md

A conversational TV avatar: WebRTC voice in, talking-head video out, and a separate control
WebSocket that drives the TV UI while the avatar speaks. Two applications in one repository —
the Python backend in `src/tv_avatar/` and the React TV frontend in `frontend/`. Everything
below is about the backend unless it says otherwise. Read `README.md`
for the architecture; the full design and its rejected alternatives are in
`docs/superpowers/specs/2026-09-19-tv-avatar-backend-design.md`. Section references in code
comments (`spec §9`, `D5`) point at that file.

## Commands

```bash
uv sync                                              # install; uv.lock is authoritative
uv run pytest                                        # 81 tests, ~3 s, no keys or network
uv run pytest tests/test_command_bus.py -k cancel    # single test
uvx ruff check src tests tools                       # lint (see note below)
uv run uvicorn tv_avatar.app:app --reload --port 8000
uv run python tools/export_schemas.py                # regenerate contracts/
uv run python tools/langfuse_smoke.py                # one span to Langfuse + read-back (needs keys)
```

Always `uv run`; never invoke `.venv/bin/python` or bare `pip` directly.

Ruff is the agreed linter (`.claude/rules.md`, 88 columns) but is **not** a declared dev
dependency and the repo carries no ruff config, so `uv run ruff` fails — use `uvx ruff`.
Scope it to `src tests tools`: a bare `ruff check .` also walks the vendored skills under
`.claude/` and drowns the real findings in hundreds of third-party ones.

## Platform

**This is a GitHub repository — use `gh`, "pull request", GitHub Actions.**
`.claude/rules.md` says GitLab and `glab`; that file is stale template content and is wrong
on this point. Its Conventional Commits and Python style sections are correct and in use.

Default branch is `main`; work lands on `dev` first. Commits follow Conventional Commits
(`feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`).

## Invariants

These are load-bearing. Breaking one is a behaviour regression, not a style choice.

- **`agent/commands.py` is the single source of truth for the command vocabulary.** The
  prompt's capability manifest, `contracts/protocol.schema.json` and `contracts/protocol.d.ts`
  are all generated from it. Adding a verb means editing that file and rerunning
  `tools/export_schemas.py` — never hand-edit anything in `contracts/`.
- **Layer boundaries.** `agent/commands.py` imports no Pipecat. `control/channel.py` knows
  the wire format but not the agent. `pipeline/builder.py` knows Pipecat but not the
  protocol. Keep it that way.
- **Commands are fire-and-forget.** Only `search_catalog` (`AWAITS_RESULT` in `commands.py`)
  blocks an LLM turn, for at most 400 ms. Awaiting the TV app anywhere else stalls speech.
- **Commands are turn-scoped.** Barge-in drops a turn's queued-but-unsent commands
  (`CommandBus.cancel_turn`). Commands already handed to the WebSocket are never rolled back.
- **Every wire message carries `"v": 1`.** An unknown version is an explicit `error`, never
  a tolerated missing field — the TV app ships on its own schedule.
- **The session id is not a credential**, the control token is. Unknown session and bad token
  must stay indistinguishable in responses.
- **No secret in source.** Everything goes through `pydantic-settings` in `config.py`;
  a new setting also goes into `.env.example` with a blank value.
- **`SessionEventsObserver` is an observer, not a processor** — it must never sit in the
  media path and add latency to speech.
- **Tracing is off the media path and off by default.** Spans are attribute writes; export is
  batched on a background thread; there is no `if tracing_enabled` in business code —
  `tracing.py`, `pipeline/builder.py` and `pipeline/runner.py` own the toggle and the session
  scope. Tests never construct a network exporter: `tests/conftest.py::otel` is the only place
  that installs a provider (OTel allows one per process).
- **Every span declares a Langfuse observation type and puts filterable facts under
  `langfuse.observation.metadata.*`.** Open spans through `tracing.observation()`, never
  `tracer.start_span` directly, and take attribute keys from the constants in `tracing.py` —
  the vocabulary table in `docs/superpowers/plans/2026-09-19-tv-avatar-observability.md` is
  the contract.

## Gotchas

Each of these cost real debugging time; the code comments record them at the call site.

- **SLNG regional routing is a header**, `X-World-Part-Override` (`world_part_override=`).
  Per-region hostnames like `eu.api.slng.ai` do not resolve.
- **`AnamVideoService` needs `api_version="v1"` explicitly.** pipecat-anam 0.2.0a6 forwards
  its own `None` default over the SDK's, producing `.../None/engine/session` → 404. Also,
  `enable_session_replay` is a service kwarg, not a `PersonaConfig` field.
- **`LLMService` validates that every `LLMSettings` field is initialised.** A subclass that
  omits them fails at construction with a confusing error — see `_stub_settings()` in
  `agent/llm.py`.
- **Turn-taking is a silence timer, not the smart-turn model.** A "sounds unfinished"
  verdict can hold a turn open indefinitely on VAD echo. `PHANTOM_TURN_TIMEOUT_S = 2.0`
  abandons a turn with VAD activity but no transcript.
- **`prerelease = "explicit"` in `pyproject.toml` is required.** pipecat-anam's modern line
  is a prerelease; the stable 0.1.0 targets legacy Pipecat and will silently resolve instead.
- **Tests run without API keys.** `create_app()` must keep starting with no `.env`;
  `_missing_settings()` absorbs only `ValidationError`, so keep other failures propagating.
- **Pytest is `asyncio_mode = "auto"`** — async tests need no decorator.
- **Langfuse ingests OTLP HTTP/protobuf only.** Never add `opentelemetry-exporter-otlp` (it
  pulls the gRPC exporter). An explicit OTLP `endpoint=` needs `/v1/traces` appended — the
  env-var path adds it, the constructor does not (`tracing.build_exporter`).
- **`opentelemetry-semantic-conventions`, `-instrumentation` and `-processor-baggage` are
  pre-releases.** Keep their explicit `>=0.54b0` markers in `pyproject.toml`; under
  `prerelease = "explicit"` uv will not resolve them otherwise.
- **Pipecat's `traced_llm` closes the `llm` span before the `InterruptionFrame` arrives** (it
  cancels the frame task first), so the interruption stamp happens in `_run_turn`'s
  cancellation handler, not in `_cancel_turn`. `AIService.setup()` also resets
  `_tracing_enabled` from the StartFrame — tests re-apply it after setup (`_traced()`).
- **A self-hosted Langfuse v3 serves `/api/public/traces` but `langfuse-cli` refuses it** as
  deprecated, and the v4 `/api/public/v2/observations` 404s there. `tools/langfuse_smoke.py`
  reads back over plain HTTP, trying both.

## Conventions

- Python 3.11+, type hints on signatures, `X | None` over `Optional[X]`.
- Pydantic v2 models for anything crossing a boundary; discriminated unions for message and
  command families.
- `loguru` for logging, never `print`.
- Comments explain *why*, especially measured numbers and library workarounds. Match the
  existing density — this codebase comments decisions, not mechanics.
- New behaviour gets a test in `tests/`, named after the behaviour rather than the function.

## Current state

M0–M2 are done (voice loop, avatar with interruption, control protocol + mock client).
The agent is still `StubLLMService` for tests plus a plain `OpenAILLMService` in production:
**no tool calls and no screen-state injection are wired yet.** That is phase 2 / M3, and the
seam is the `TODO(phase 2)` in `pipeline/builder.py` — `ScreenContextInjector` belongs
between the user aggregator and the LLM so `SessionState.render_for_prompt()` is injected
fresh on every run.

## The frontend

`frontend/` is a separate application with its own toolchain (`npm`, Vite, Node 20.19+) and
its own `README.md`, `PRODUCT.md` and `design.md`. Do not run `uv` in it or `npm` outside it.

It is **not yet connected to this backend**: it speaks no control protocol, and Watch,
Episodes and Continue only record local history. Wiring those seams to `contracts/protocol.d.ts`
is the work that joins the two halves — and the generated TypeScript is what it should import
rather than hand-writing the command shapes.
