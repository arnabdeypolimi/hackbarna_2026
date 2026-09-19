"""Templated answer for when a follow-up cycle blows its first-byte budget:
the tool results are spoken without an LLM call so the turn still ends in speech."""
from tv_avatar.agent.turn import ToolResult


def render_fallback(results: tuple[ToolResult, ...]) -> tuple[str, list[tuple[str, dict]]]:
    """Spoken answer + TV actions built from tool results without an LLM call."""
    for verb, result in ((r.verb, r.payload) for r in results):
        if verb == "recommend_titles":
            titles = result.get("titles") or []
            if not titles:
                return ("I couldn't find anything matching that right now. Want to try something else?", [])
            names = [f"{t['name']} from {t['year']}" if t.get("year") else t["name"] for t in titles[:3]]
            spoken = names[0] if len(names) == 1 else ", ".join(names[:-1]) + f", or {names[-1]}"
            return (f"How about {spoken}?", [("focus", {"title_id": titles[0]["title_id"]})])
        if verb == "recall_memory":
            memory = (result.get("memory") or "").strip()
            if memory and memory != "(none yet)":
                return (f"Here's what I remember: {memory.splitlines()[-1].lstrip('- ')}", [])
            return ("I don't have that in my memory yet.", [])
    return ("Sorry, that took too long. Could you say it again?", [])
