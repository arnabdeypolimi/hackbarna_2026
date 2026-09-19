"""The SGR turn envelope: one Pydantic schema IS the turn contract (D8).

`intent` routes, `say` is the spoken reply (ordered before actions so filler
reaches TTS while actions generate), `actions[]` is a discriminated union built
from COMMAND_MODELS plus the internal tools — phase 1's no-drift rule holds.
"""
from copy import deepcopy
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field

from tv_avatar.agent.commands import AWAITS_RESULT, COMMAND_MODELS, Verb

Genre = Literal[
    "Action", "Adventure", "Animation", "Comedy", "Crime", "Documentary", "Drama", "Family",
    "Fantasy", "History", "Horror", "Music", "Mystery", "Romance", "Science Fiction", "TV Movie",
    "Thriller", "War", "Western",
]
TMDB_GENRES: tuple[str, ...] = Genre.__args__  # type: ignore[attr-defined]


class RecommendTitles(BaseModel):
    """Internal: ask the recommendation engine for titles; results feed cycle 2.

    Constraints are typed so constrained decoding can only emit real TMDB genres;
    the model — not a keyword list — decides what the user's memory implies."""
    verb: Literal["recommend_titles"] = "recommend_titles"
    query: str | None = None
    genres: list[Genre] = Field(default_factory=list, description="wanted genres (any match)")
    exclude_genres: list[Genre] = Field(
        default_factory=list,
        description="genres to never return — fill from Memory dislikes (e.g. 'dislikes horror' -> Horror)")
    year_min: int | None = None
    year_max: int | None = None
    similar_to: str | None = None
    limit: int = Field(default=8, ge=1, le=12)


class RecallMemory(BaseModel):
    """Internal: search conversational memory for something the user told us before."""
    verb: Literal["recall_memory"] = "recall_memory"
    query: str


class RejectTitle(BaseModel):
    """Internal: the viewer declined a title that was offered. Recorded in the
    viewing log so it is not recommended again or offered at the next greeting.
    Fire-and-forget — no result comes back and no second cycle follows."""
    verb: Literal["reject_title"] = "reject_title"
    title_id: str


INTERNAL_MODELS: dict[str, type[BaseModel]] = {
    "recommend_titles": RecommendTitles,
    "recall_memory": RecallMemory,
    "reject_title": RejectTitle,
}
#: Internal verbs whose result the turn waits for (and that earn a second cycle).
INTERNAL_AWAIT: frozenset[str] = frozenset({"recommend_titles", "recall_memory"})

ALL_MODELS: dict[str, type[BaseModel]] = {
    **{v.value: m for v, m in COMMAND_MODELS.items()},
    **INTERNAL_MODELS,
}
AWAITED_VERBS: frozenset[str] = frozenset({v.value for v in AWAITS_RESULT}) | INTERNAL_AWAIT

ActionUnion = Annotated[Union[tuple(ALL_MODELS.values())], Field(discriminator="verb")]  # noqa: UP007

Intent = Literal["control", "navigate", "recommend", "search", "answer", "chitchat", "clarify"]


class TurnPlan(BaseModel):
    intent: Intent
    say: str
    actions: list[ActionUnion] = Field(default_factory=list)


# --- response_format schema ------------------------------------------------

_DROP_KEYS = frozenset({"title", "default", "discriminator"})


def _strictify(node: Any) -> Any:
    """Make a pydantic schema acceptable to strict constrained decoders:
    every object closed and fully required, oneOf → anyOf, no defaults/titles."""
    if isinstance(node, list):
        return [_strictify(n) for n in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _DROP_KEYS:
            continue
        if key == "oneOf":
            key = "anyOf"
        out[key] = _strictify(value)
    if out.get("type") == "object" and "properties" in out:
        out["additionalProperties"] = False
        out["required"] = list(out["properties"])
    return out


def turn_plan_schema() -> dict:
    schema = _strictify(deepcopy(TurnPlan.model_json_schema()))
    return {"name": "turn_plan", "strict": True, "schema": schema}


# --- capability manifest ---------------------------------------------------

_VERB_DOCS: dict[str, str] = {
    Verb.PLAY: "start playing a title from the screen or a recommendation",
    Verb.PAUSE: "pause playback",
    Verb.RESUME: "resume playback",
    Verb.SEEK: "jump to an absolute time or by a delta",
    Verb.NAVIGATE: "move the focus up/down/left/right",
    Verb.FOCUS: "highlight a tile by title_id",
    Verb.OPEN_DETAILS: "open the details page of a title",
    Verb.CLOSE: "close the current overlay/details",
    Verb.BACK: "go back one screen",
    Verb.HOME: "return to the home grid",
    Verb.SHOW_PRODUCTS: "show products visible in a scene",
    Verb.SEARCH_CATALOG: "free-text catalog search on the TV; returns results to you",
    Verb.SHOW_TITLES: "put a labelled rail of titles on screen, first one focused — for recommendations and search hits",
    "recommend_titles": "INTERNAL — ask the recommendation engine; you receive titles and then speak them",
    "recall_memory": "INTERNAL — look up something the user told you in the past",
    "reject_title": "INTERNAL — the user declined a title you offered; it will not be offered again",
}


def _field_sig(model: type[BaseModel]) -> str:
    parts = []
    for name, info in model.model_fields.items():
        if name == "verb":
            continue
        ann = getattr(info.annotation, "__name__", None) or str(info.annotation).replace("typing.", "")
        parts.append(f"{name}: {ann}" + ("" if info.is_required() else " (optional)"))
    return ", ".join(parts) or "no arguments"


def describe_capabilities() -> str:
    lines = []
    for verb, model in ALL_MODELS.items():
        tag = " [awaits result]" if verb in AWAITED_VERBS else ""
        lines.append(f"- {verb}{tag}: {_VERB_DOCS.get(verb, '')} — {_field_sig(model)}")
    return "\n".join(lines)
