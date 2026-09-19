# TV avatar backend

Low-latency voice agent for a TV: Pipecat pipeline (SLNG speech I/O, Anam avatar), a
Schema-Guided-Reasoning agent over an OpenAI-compatible LLM (Nebius), a per-user
memory profile rewritten at the end of every session, and TMDB recommendations. See `docs/superpowers/` for the spec and
plans, `docs/findings/` for measured behaviour.

## Setup

```bash
uv sync
cp .env.example .env        # fill OPENAI_API_KEY (+ SLNG_*/ANAM_* for the spoken path)
```

`data/` is git-ignored; the TMDB CSV goes to `data/TMDB_movie_dataset_v11.csv`.

## Build the catalog (once per dataset)

```bash
uv run python tools/build_catalog.py --limit 1000   # smoke slice
uv run python tools/build_catalog.py                # full run (CATALOG_INDEX_LIMIT)
```

Writes `data/catalog.parquet` + `data/qdrant_db/`. Resumable.

## Run

```bash
AGENT_IMPL=sgr uv run uvicorn tv_avatar.app:app --reload
```

Open `http://localhost:8000/mock/`, press **Talk**. The mock client loads real tiles from
`GET /catalog/sample`, keeps a `user_id` in localStorage (personalisation survives sessions),
shows captions and agent status from the control socket, and negotiates WebRTC via
`POST /sessions/{id}/offer`.

`AGENT_IMPL=stub` (default) runs the deterministic phase-1 stub through the same pipeline.

## Text-mode smoke (no speech keys)

```bash
uv run python tools/smoke_turn.py
uv run python tools/smoke_turn.py --user couch_1 "something like Sicario" "play the first one"
```

Drives the real agent, catalog, memory lane and history with typed turns; prints what would
have been spoken, the TV commands, and per-turn timings.

## Tests

```bash
uv run pytest                                  # no network
```

Logs are loguru-only; every in-session line is bound with `session_id`/`user_id`/`turn_id`
(`grep turn_id=` reconstructs a turn). `LOG_LEVEL=DEBUG` shows prefetch hits/misses.
