"""The SGR turn envelope: one Pydantic schema IS the turn contract (D8).

`intent` routes, `say` is the spoken reply (ordered before actions so filler
reaches TTS while actions generate), `actions[]` is a discriminated union built
from COMMAND_MODELS plus the internal tools — phase 1's no-drift rule holds.

There is deliberately no free-text "thoughts" slot ahead of `say`: every token
before the first spoken byte is silence the viewer hears. `intent` is the
cascade's reasoning step.

REGISTRY is the one place that says what each verb *is*: TV command or internal
tool, whether the turn blocks on its result, and how the capability manifest
describes it. An internal awaited tool *returns an observation* — a reply the
model must see, which is what buys the next cycle.

The cycle cap is enforced by the schema, not the loop: the final cycle is decoded
against `turn_plan_schema(final=True)`, whose actions union simply has no
observation-returning tools, so constrained decoding cannot over-call.
"""
from copy import deepcopy
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter

from tv_avatar.agent.commands import AWAITS_RESULT, COMMAND_MODELS, Verb
from tv_avatar.ambient.scenes import describe_scenes

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


class RejectTitle(BaseModel):
    """Internal: the viewer declined a title that was offered. Recorded in the
    viewing log so it is not recommended again or offered at the next greeting.
    Fire-and-forget — no result comes back and no second cycle follows."""
    verb: Literal["reject_title"] = "reject_title"
    title_id: str


InternalAction = RecommendTitles | RejectTitle


@dataclass(frozen=True)
class ActionSpec:
    model: type[BaseModel]
    kind: Literal["tv", "internal"]
    #: The turn blocks on this action's reply (TV round-trip or internal tool).
    awaits_result: bool
    doc: str

    @property
    def returns_observation(self) -> bool:
        """Awaited replies feed another LLM cycle, whether from an internal tool
        or a TV search. The final cycle excludes these actions from its schema."""
        return self.awaits_result


_TV_DOCS: dict[Verb, str] = {
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
    Verb.SHOW_PRODUCTS: "slide in the shop shelf for a title — its outfits and merchandise; only for title_ids listed in the Shop section",
    Verb.SEARCH_CATALOG: "free-text catalog search on the TV; returns results to you",
    Verb.SHOW_TITLES: "put a labelled rail of titles on screen, first one focused — for recommendations and search hits",
    Verb.SHOW_AMBIENT: "show a relaxing looping video full screen with its own sound; scenes: " + describe_scenes(),
    Verb.HIDE_AMBIENT: "close the ambient video and return to browsing",
}

REGISTRY: dict[str, ActionSpec] = {
    **{v.value: ActionSpec(m, "tv", awaits_result=v in AWAITS_RESULT, doc=_TV_DOCS[v])
       for v, m in COMMAND_MODELS.items()},
    "recommend_titles": ActionSpec(
        RecommendTitles, "internal", awaits_result=True,
        doc="INTERNAL — ask the recommendation engine; you receive titles and then speak them"),
    "reject_title": ActionSpec(
        RejectTitle, "internal", awaits_result=False,
        doc="INTERNAL — the user declined a title you offered; it will not be offered again"),
}

ActionUnion = Annotated[Union[tuple(s.model for s in REGISTRY.values())], Field(discriminator="verb")]  # noqa: UP007
#: The final cycle's vocabulary: everything that does not return an observation.
FinalActionUnion = Annotated[
    Union[tuple(s.model for s in REGISTRY.values() if not s.returns_observation)],  # noqa: UP007
    Field(discriminator="verb")]
_ACTION_ADAPTER: TypeAdapter = TypeAdapter(ActionUnion)
_FINAL_ACTION_ADAPTER: TypeAdapter = TypeAdapter(FinalActionUnion)


def parse_action(raw: dict[str, Any], *, final: bool = False) -> BaseModel:
    """One streamed `actions[]` element → its typed model, validated against the
    union the cycle was decoded with. Raises ValidationError."""
    return (_FINAL_ACTION_ADAPTER if final else _ACTION_ADAPTER).validate_python(raw)

Intent = Literal["control", "navigate", "recommend", "search", "answer", "chitchat", "clarify"]


class TurnPlan(BaseModel):
    intent: Intent
    say: str
    actions: list[ActionUnion] = Field(default_factory=list)


class FinalTurnPlan(BaseModel):
    intent: Intent
    say: str
    actions: list[FinalActionUnion] = Field(default_factory=list)


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


def turn_plan_schema(*, final: bool = False) -> dict:
    model, name = (FinalTurnPlan, "turn_plan_final") if final else (TurnPlan, "turn_plan")
    return {"name": name, "strict": True, "schema": _strictify(deepcopy(model.model_json_schema()))}


# --- capability manifest ---------------------------------------------------

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
    for verb, spec in REGISTRY.items():
        tag = " [awaits result]" if spec.awaits_result else ""
        lines.append(f"- {verb}{tag}: {spec.doc} — {_field_sig(spec.model)}")
    return "\n".join(lines)
