"""The SGR turn envelope: one Pydantic schema IS the turn contract (D8).

The cascade is the field order: `intent` routes, `request` decodes what the
viewer asked to have done (operation, named title, its id if known), `say` is
the spoken reply (ahead of actions so filler reaches TTS while actions
generate), `actions[]` is a discriminated union built from COMMAND_MODELS plus
the internal tools — phase 1's no-drift rule holds.

There is deliberately no free-text "thoughts" slot ahead of `say`: every token
before the first spoken byte is silence the viewer hears. `intent` and
`request` are the cascade's reasoning steps, and `request` is checked against
every action as it streams (`action_violation`): a valid envelope whose actions
serve a *different* operation was the field failure — "watch Barbie" answered
with recommendations (2026-09-20).

REGISTRY is the one place that says what each verb *is*: TV command or internal
tool, whether the turn blocks on its result, and how the capability manifest
describes it. An internal awaited tool *returns an observation* — a reply the
model must see, which is what buys the next cycle.

The cycle cap is enforced by the schema, not the loop: the final cycle is decoded
against `turn_plan_schema(final=True)`, whose actions union simply has no
observation-returning tools, so constrained decoding cannot over-call.
"""
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter

from tv_avatar.agent.commands import AWAITS_RESULT, COMMAND_MODELS, Verb
from tv_avatar.ambient.scenes import describe_scenes
from tv_avatar.sgr import response_format

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
    Verb.PLAY: "start playback of a title by title_id",
    Verb.PAUSE: "pause playback",
    Verb.RESUME: "resume playback",
    Verb.SEEK: "skip ahead/back N seconds -> delta_seconds (never add it to the position); "
               "go to a time -> to_seconds",
    Verb.NAVIGATE: "move the focus up/down/left/right",
    Verb.FOCUS: "highlight a tile by title_id",
    Verb.OPEN_DETAILS: "open a title's details page, on or off screen",
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
        doc="INTERNAL — discover titles; similar_to EXCLUDES that movie; results come back to you"),
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

#: What the viewer asked to have done, decoded before `say` so it steers what follows.
Operation = Literal["play", "open", "lookup", "discover", "shop", "control", "answer"]


class Request(BaseModel):
    # The descriptions ride into the decoding schema: the model reads them at the
    # moment it commits to the operation, which is where "you pick" went wrong.
    operation: Operation = Field(description=(
        "play/open/shop need one specific movie the viewer named or pointed at on screen "
        "('the second one'). A mood, a genre, 'you pick', 'surprise me' or 'something ...' "
        "is discover, even when they say 'put something on'."))
    #: The movie named by the viewer, verbatim; None when they named none.
    title: str | None = Field(description="The movie the viewer named, verbatim; null when they named none.")
    #: Its catalog id when a supplied section or tool result lists it, else
    #: None — the cue to search rather than guess or substitute.
    title_id: str | None = Field(description=(
        "Its id when the Screen, Shop, Recent activity, Recommendations or a tool result lists it; "
        "null means search first, never guess."))


class TurnPlan(BaseModel):
    intent: Intent
    request: Request
    say: str
    actions: list[ActionUnion] = Field(default_factory=list)


class FinalTurnPlan(BaseModel):
    intent: Intent
    request: Request
    say: str
    actions: list[FinalActionUnion] = Field(default_factory=list)


#: Verbs with a consequence the viewer did not ask for unless the request
#: licenses them. Everything else (search, rails, focus, transport) is free:
#: a rail of candidates or a focus move is harmless whatever the request.
_LICENSED_BY: dict[str, frozenset[str]] = {
    "play": frozenset({"play"}),
    "open_details": frozenset({"open", "lookup"}),
    "show_products": frozenset({"shop"}),
    "recommend_titles": frozenset({"discover"}),
    "reject_title": frozenset({"discover"}),
}
#: Title-directed verbs must target the id the request decoded — and there
#: must be one: a null `title_id` means "search first", never "guess".
_TITLE_DIRECTED = frozenset({"play", "open_details", "show_products"})


def action_violation(request: Request, action: BaseModel) -> str | None:
    """Why this action does not serve the decoded request, or None if it does.

    Checked per action as it streams (the cascade puts `request` before
    `actions`, so it is known by then). The schema cannot express "these
    actions match that operation", so this is the SGR validation step that
    makes the decoded request binding rather than advisory.
    """
    verb = str(action.verb)
    licensed = _LICENSED_BY.get(verb)
    if licensed is not None and request.operation not in licensed:
        article = "an" if request.operation in ("open", "answer") else "a"
        return f"{verb} does not serve {article} {request.operation} request"
    if verb in _TITLE_DIRECTED:
        target = getattr(action, "title_id", None)
        if request.title_id is None:
            return f"{verb} before the title's id is known"
        if target != request.title_id:
            return f"{verb} targets {target}, not the requested {request.title_id}"
    return None


def plan_violations(plan: TurnPlan | FinalTurnPlan) -> list[str]:
    return [reason for a in plan.actions if (reason := action_violation(plan.request, a))]


# --- response_format schema ------------------------------------------------

def turn_plan_schema(*, final: bool = False, operation: str | None = None) -> dict:
    """The decoding contract for one cycle.

    ``operation`` pins ``request.operation`` to a single value for a follow-up
    cycle: the viewer asked once, in cycle 1, and the results coming back cannot
    change what they asked for. Seen live: "You pick. Crime and thrillers" decoded
    as discover, then the cycle that received the recommendations re-decoded the
    request as play and started the top hit. With the const in the schema that
    envelope is undecodable; the request gate then holds against the pin as well.
    """
    model, name = (FinalTurnPlan, "turn_plan_final") if final else (TurnPlan, "turn_plan")
    schema = response_format(model, name)
    if operation is not None:
        # `request` is a $ref into $defs (Pydantic emits nested models that way);
        # the const goes on the definition, and the plan's own field description
        # is kept so the model still reads why the operation is what it is.
        schema["schema"]["$defs"]["Request"]["properties"]["operation"] = {
            "const": operation, "type": "string",
            "description": f"Fixed for this turn: the viewer asked to {operation}."}
        schema["name"] = f"{name}_{operation}"
    return schema


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
