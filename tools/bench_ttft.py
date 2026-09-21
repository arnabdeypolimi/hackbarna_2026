"""Voice-agent latency bench for every Nebius Token Factory text model: time to first
token, time to first speakable sentence (~12 words, what TTS needs to start) and
decode speed, cold (system + one utterance) and warm (same, behind ~3k tokens of
dialogue history). Prints a markdown table; medians of N runs. Never prints keys.

Run: set -a; . ./.env; set +a; uv run --with openai python tools/bench_ttft.py [runs] [substr ...]
Optional substrings restrict the run to matching model ids (e.g. `3 kimi minimax`).
"""
import asyncio
import statistics
import sys
import time

from openai import AsyncOpenAI

c = AsyncOpenAI()
RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 3
ONLY = [s.lower() for s in sys.argv[2:]]
SKIP = {"Qwen/Qwen3-Embedding-8B", "openbmb/MiniCPM-V-4_5"}
FIRST_SENTENCE_WORDS = 12

# Thinking-off switch per model family; reasoning models otherwise think for seconds.
def extra_for(model: str) -> dict:
    m = model.lower()
    if any(k in m for k in ("deepseek", "kimi", "minimax")):
        return {"chat_template_kwargs": {"thinking": False}}
    if "gpt-oss" in m:
        return {"reasoning_effort": "low"}
    if any(k in m for k in ("glm", "qwen3.5", "nemotron", "hermes")):
        return {"chat_template_kwargs": {"enable_thinking": False}}
    return {}

SYS = ("You are Ada, a friendly voice assistant on a TV. Answer in one or two short spoken "
       "sentences, no lists, no markdown. Screen: [0] Heat (1995) <- focused, [1] Sicario, "
       "[2] Collateral, [3] Drive. The user is on the sofa and can only hear you.")
USER = "hmm, which of these would you pick for tonight and why?"

def history(turns: int = 28) -> list[dict]:
    """~3k tokens of plausible prior dialogue."""
    qa = [
        ("what's on tonight?", ("Tonight the row is all crime thrillers: Heat, Sicario, Collateral and Drive. "
         "Heat is the long one, Drive is the shortest at about an hour and a half.")),
        ("who's in Heat again?", ("Al Pacino and Robert De Niro, with Val Kilmer, and Michael Mann directing. "
         "It's the famous one with the diner scene and the downtown shootout.")),
        ("is Sicario very violent?", ("It's tense more than gory, but there are a few hard scenes. "
         "Emily Blunt plays an FBI agent pulled into a cartel task force with Benicio del Toro.")),
        ("remind me what I watched last week", ("You finished Mindhunter season two and the first half of "
         "The Bear. You also started Collateral but stopped around twenty minutes in.")),
    ]
    out = []
    for i in range(turns):
        q, a = qa[i % len(qa)]
        out += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
    return out

async def one(model: str, msgs: list[dict]) -> dict:
    t0 = time.perf_counter(); ttft = tfs = None; words = 0; toks = 0; reason = 0; last = t0
    stream = await c.chat.completions.create(
        model=model, stream=True, max_tokens=80, temperature=0.3, messages=msgs,
        extra_body=extra_for(model) or None)
    async for ch in stream:
        if not ch.choices:
            continue
        d = ch.choices[0].delta
        if getattr(d, "reasoning_content", None):
            reason += len(d.reasoning_content)
        if d.content:
            now = time.perf_counter()
            if ttft is None:
                ttft = now - t0
            toks += 1; last = now
            words += d.content.count(" ")
            if tfs is None and words >= FIRST_SENTENCE_WORDS:
                tfs = now - t0
    total = time.perf_counter() - t0
    tps = (toks - 1) / (last - (t0 + ttft)) if ttft is not None and toks > 1 and last > t0 + ttft else 0
    return {"ttft": ttft, "tfs": tfs or total, "tps": tps, "reason": reason, "total": total}

async def scenario(model: str, msgs: list[dict]) -> dict | str:
    rows = []
    for _ in range(RUNS):
        try:
            rows.append(await one(model, msgs))
        except Exception as e:  # noqa: BLE001 — bench: record and move on
            return f"ERR {type(e).__name__}: {str(e)[:60]}"
    if any(r["ttft"] is None for r in rows):
        return "no content (all thinking?)"
    med = lambda k: statistics.median(r[k] for r in rows)
    return {k: med(k) for k in ("ttft", "tfs", "tps", "reason")}

async def bench(model: str, sem: asyncio.Semaphore) -> tuple[str, dict | str, dict | str]:
    short = [{"role": "system", "content": SYS}, {"role": "user", "content": USER}]
    long = [short[0], *history(), short[1]]
    async with sem:
        s = await scenario(model, short)
        l = await scenario(model, long)
    print(f"  done {model}", file=sys.stderr)
    return model, s, l

def fmt(r: dict | str) -> str:
    if isinstance(r, str):
        return f"{r} | | "
    think = " (thought)" if r["reason"] else ""
    return f"{r['ttft']*1000:.0f}{think} | {r['tfs']*1000:.0f} | {r['tps']:.0f}"

async def main() -> None:
    models = sorted(m.id for m in (await c.models.list()).data
                    if m.id not in SKIP and (not ONLY or any(s in m.id.lower() for s in ONLY)))
    # long-prompt token count, once, for the header
    probe = await c.chat.completions.create(
        model="Qwen/Qwen3-30B-A3B-Instruct-2507", max_tokens=1,
        messages=[{"role": "system", "content": SYS}, *history(), {"role": "user", "content": USER}])
    ptoks = probe.usage.prompt_tokens
    sem = asyncio.Semaphore(4)
    res = await asyncio.gather(*(bench(m, sem) for m in models))
    res.sort(key=lambda r: (r[1]["ttft"] if isinstance(r[1], dict) else 9e9))
    print(f"\nMedian of {RUNS} runs, ms. Short = system + 1 utterance (~130 tok). "
          f"History = same behind {ptoks} prompt tokens of dialogue. max_tokens=80, thinking off.\n")
    print("| Model | TTFT short | 1st sentence short | tok/s | TTFT history | 1st sentence history | tok/s |")
    print("|---|---|---|---|---|---|---|")
    for m, s, l in res:
        print(f"| {m} | {fmt(s)} | {fmt(l)} |")

asyncio.run(main())
