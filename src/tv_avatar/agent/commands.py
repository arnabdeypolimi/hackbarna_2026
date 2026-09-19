"""Command vocabulary. Single source of truth for spec §8.

The prompt's capability manifest, the JSON Schema artifact, and the
TypeScript types for the TV app are all generated from these models.
Nothing downstream may hand-write a command shape.
"""
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, model_validator


class Verb(StrEnum):
    PLAY = "play"
    PAUSE = "pause"
    RESUME = "resume"
    SEEK = "seek"
    NAVIGATE = "navigate"
    FOCUS = "focus"
    OPEN_DETAILS = "open_details"
    CLOSE = "close"
    BACK = "back"
    HOME = "home"
    SHOW_PRODUCTS = "show_products"
    SEARCH_CATALOG = "search_catalog"


class Play(BaseModel):
    verb: Literal[Verb.PLAY] = Verb.PLAY
    title_id: str
    resume_from: float | None = Field(default=None, ge=0)


class Pause(BaseModel):
    verb: Literal[Verb.PAUSE] = Verb.PAUSE


class Resume(BaseModel):
    verb: Literal[Verb.RESUME] = Verb.RESUME


class Seek(BaseModel):
    verb: Literal[Verb.SEEK] = Verb.SEEK
    to_seconds: float | None = Field(default=None, ge=0)
    delta_seconds: float | None = None

    @model_validator(mode="after")
    def exactly_one_target(self) -> Self:
        if (self.to_seconds is None) == (self.delta_seconds is None):
            raise ValueError("seek needs exactly one of to_seconds or delta_seconds")
        return self


class Navigate(BaseModel):
    verb: Literal[Verb.NAVIGATE] = Verb.NAVIGATE
    direction: Literal["up", "down", "left", "right"]
    count: int = Field(default=1, ge=1, le=20)


class Focus(BaseModel):
    verb: Literal[Verb.FOCUS] = Verb.FOCUS
    title_id: str


class OpenDetails(BaseModel):
    verb: Literal[Verb.OPEN_DETAILS] = Verb.OPEN_DETAILS
    title_id: str


class Close(BaseModel):
    verb: Literal[Verb.CLOSE] = Verb.CLOSE


class Back(BaseModel):
    verb: Literal[Verb.BACK] = Verb.BACK


class Home(BaseModel):
    verb: Literal[Verb.HOME] = Verb.HOME


class ShowProducts(BaseModel):
    verb: Literal[Verb.SHOW_PRODUCTS] = Verb.SHOW_PRODUCTS
    title_id: str
    scene_at: float | None = Field(default=None, ge=0)


class SearchCatalog(BaseModel):
    verb: Literal[Verb.SEARCH_CATALOG] = Verb.SEARCH_CATALOG
    query: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=10, ge=1, le=50)


CommandArgs = Annotated[
    Play | Pause | Resume | Seek | Navigate | Focus
    | OpenDetails | Close | Back | Home | ShowProducts | SearchCatalog,
    Field(discriminator="verb"),
]

COMMAND_MODELS: dict[Verb, type[BaseModel]] = {
    Verb.PLAY: Play,
    Verb.PAUSE: Pause,
    Verb.RESUME: Resume,
    Verb.SEEK: Seek,
    Verb.NAVIGATE: Navigate,
    Verb.FOCUS: Focus,
    Verb.OPEN_DETAILS: OpenDetails,
    Verb.CLOSE: Close,
    Verb.BACK: Back,
    Verb.HOME: Home,
    Verb.SHOW_PRODUCTS: ShowProducts,
    Verb.SEARCH_CATALOG: SearchCatalog,
}

#: Only these block the LLM turn awaiting a client response (spec §8).
AWAITS_RESULT: frozenset[Verb] = frozenset({Verb.SEARCH_CATALOG})


def parse_command(verb: str, args: dict) -> CommandArgs:
    """Validate a verb and its arguments. Raises ValueError on either failure."""
    try:
        v = Verb(verb)
    except ValueError as exc:
        raise ValueError(f"unknown verb: {verb!r}") from exc
    return COMMAND_MODELS[v].model_validate({**args, "verb": v})
