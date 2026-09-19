"""Bench Nebius candidates for the phase-2 turn brain: TTFT + total for a streamed
json_schema envelope with anyOf actions, plus embedding latency. Prints model ids
and timings only; never prints keys. Reads OPENAI_API_KEY / OPENAI_BASE_URL.

Run: set -a; . ./.env; set +a; uv run --with openai python tools/bench_llm.py
"""
import json, os, sys, time
from openai import OpenAI

c = OpenAI()
SCHEMA = {"name": "turn_plan", "strict": True, "schema": {
    "type": "object", "additionalProperties": False,
    "required": ["intent", "say", "actions"],
    "properties": {
        "intent": {"type": "string", "enum": ["control", "recommend", "chitchat", "clarify"]},
        "say": {"type": "string"},
        "actions": {"type": "array", "items": {"anyOf": [
            {"type": "object", "additionalProperties": False, "required": ["verb", "title_id"],
             "properties": {"verb": {"type": "string", "enum": ["play"]}, "title_id": {"type": "string"}}},
            {"type": "object", "additionalProperties": False, "required": ["verb"],
             "properties": {"verb": {"type": "string", "enum": ["pause"]}}},
            {"type": "object", "additionalProperties": False, "required": ["verb", "query"],
             "properties": {"verb": {"type": "string", "enum": ["recommend_titles"]}, "query": {"type": "string"}}},
        ]}}}}}
SYS = ("You are a TV assistant. Reply ONLY as compact JSON matching the schema, no prose, no markdown. Put a short spoken "
       "sentence in `say` first, then actions. Screen: [0] Heat (id=949) <- focused, [1] Sicario (id=273481). "
       "Never invent ids.")
USER = "play the second one and find me something like it"

# (model, extra_body) — thinking-off switches per family
CANDS = [
    ("deepseek-ai/DeepSeek-V4.1-Flash", {"thinking": {"type": "disabled"}}),
    ("deepseek-ai/DeepSeek-V4.1-Flash", {"chat_template_kwargs": {"thinking": False}}),
    ("deepseek-ai/DeepSeek-V4-Flash-0731", {"chat_template_kwargs": {"thinking": False}}),
    ("zai-org/GLM-5.3-Flash", {"chat_template_kwargs": {"enable_thinking": False}}),
    ("Qwen/Qwen3-30B-A3B-Instruct-2507", {}),
    ("nvidia/Nemotron-3_5-Lightning", {"chat_template_kwargs": {"enable_thinking": False}}),
    ("nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B", {"chat_template_kwargs": {"enable_thinking": False}}),
    ("openai/gpt-oss-120b", {"reasoning_effort": "low"}),
    ("google/gemma-3-27b-it", {}),
]

def bench(model, extra, runs=2):
    rows = []
    for _ in range(runs):
        t0 = time.perf_counter(); ttft = None; buf = []; reason = 0; fin = None
        try:
            stream = c.chat.completions.create(
                model=model, stream=True, max_tokens=600, temperature=0,
                messages=[{"role": "system", "content": SYS}, {"role": "user", "content": USER}],
                response_format={"type": "json_schema", "json_schema": SCHEMA},
                extra_body=extra or None)
            for ch in stream:
                if not ch.choices: continue
                d = ch.choices[0].delta
                if getattr(d, "reasoning_content", None): reason += len(d.reasoning_content)
                if d.content:
                    if ttft is None: ttft = time.perf_counter() - t0
                    buf.append(d.content)
                if ch.choices[0].finish_reason: fin = ch.choices[0].finish_reason
            total = time.perf_counter() - t0
            out = "".join(buf).strip()
            try: j = json.loads(out); ok = "valid"; order = list(j)[:2] == ["intent", "say"]
            except Exception: ok = "INVALID"; order = False
            rows.append((ttft, total, ok, order, reason, fin, out))
        except Exception as e:
            rows.append((None, None, f"ERR {type(e).__name__}", False, 0, str(e)[:90], ""))
    best = min(rows, key=lambda r: r[1] or 1e9)
    ttft, total, ok, order, reason, fin, out = best
    tag = f"{model} {json.dumps(extra)}"
    print(f"{tag[:78]:78s} ttft={(ttft or -1)*1000:6.0f} total={(total or -1)*1000:6.0f} {ok:8s} order={order} reason_chars={reason} fin={fin}")
    if ok != "valid": print(f"      -> {out[:140]!r}")

for m, x in CANDS: bench(m, x)

for n in (1, 8):
    t0 = time.perf_counter()
    e = c.embeddings.create(model="Qwen/Qwen3-Embedding-8B", input=["heist thriller like Heat"] * n)
    print(f"Qwen/Qwen3-Embedding-8B x{n}: {(time.perf_counter()-t0)*1000:.0f}ms dims={len(e.data[0].embedding)}")
