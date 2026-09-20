"""Templated answer for when a follow-up cycle blows its first-byte budget:
the tool results are spoken without an LLM call so the turn still ends in speech."""
from tv_avatar.agent.envelope import REGISTRY
from tv_avatar.agent.turn import ToolResult


def render_fallback(results: tuple[ToolResult, ...]) -> tuple[str, list[tuple[str, dict]]]:
    """Spoken answer + TV actions built from tool results without an LLM call."""
    for r in results:
        verb, result = r.verb, r.payload
        if r.failed:  # before the titles branch: a timed-out tool has no titles but did not "find nothing"
            spec = REGISTRY.get(verb)
            what = "the TV didn't respond to that" if spec is not None and spec.kind == "tv" else "that didn't go through"
            return (f"Sorry, {what}. Could you try again?", [])
        if verb == "recommend_titles" or (verb == "search_catalog" and "titles" in result):
            titles = result.get("titles") or []
            if not titles:
                return ("I couldn't find anything matching that right now. Want to try something else?", [])
            names = [f"{t['name']} from {t['year']}" if t.get("year") else t["name"] for t in titles[:3]]
            spoken = names[0] if len(names) == 1 else ", ".join(names[:-1]) + f", or {names[-1]}"
            lead = "How about" if verb == "recommend_titles" else "I found"
            label = "For you" if verb == "recommend_titles" else "Search results"
            return (f"{lead} {spoken}{'?' if verb == 'recommend_titles' else '.'}",
                    [("show_titles", {"title_ids": [t["title_id"] for t in titles[:20]], "label": label})])
    return ("Sorry, that took too long. Could you say it again?", [])
