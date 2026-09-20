"""Wire protocol between this backend and the separately deployed TV app.

Every message carries "v". The TV app ships on its own schedule, so an
unknown version is an explicit error, never a silently missing field.
"""
import json
import time
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

PROTOCOL_VERSION = 1


class ProtocolError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --- supporting shapes -------------------------------------------------

class Tile(BaseModel):
    title_id: str
    name: str
    position: int = Field(ge=0)
    #: The TV has a product shelf for this title. Commands are fire-and-forget, so a
    #: failed `show_products` ack never reaches the model; this is how it knows in
    #: advance whether "what's that jacket?" has an answer or an apology.
    shoppable: bool = False


class Playback(BaseModel):
    state: Literal["stopped", "playing", "paused"]
    title_id: str | None = None
    position_s: float = Field(default=0.0, ge=0)


class ScreenState(BaseModel):
    view: Literal["grid", "details", "player", "products"]
    rail_id: str | None = None
    focus_index: int | None = Field(default=None, ge=0)
    tiles: list[Tile] = Field(default_factory=list)
    playback: Playback


# --- client -> server --------------------------------------------------

class _Base(BaseModel):
    v: Literal[1] = PROTOCOL_VERSION


class ScreenStateMsg(_Base):
    type: Literal["screen_state"] = "screen_state"
    state: ScreenState


class AckMsg(_Base):
    type: Literal["ack"] = "ack"
    command_id: str
    ok: bool
    error: str | None = None


class ResultMsg(_Base):
    type: Literal["result"] = "result"
    command_id: str
    data: dict[str, Any]


class UserEventMsg(_Base):
    type: Literal["user_event"] = "user_event"
    event: str
    detail: dict[str, Any] = Field(default_factory=dict)


ClientMessage = Annotated[
    ScreenStateMsg | AckMsg | ResultMsg | UserEventMsg,
    Field(discriminator="type"),
]
_client_adapter: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)


# --- server -> client --------------------------------------------------

class CommandMsg(_Base):
    type: Literal["command"] = "command"
    id: str
    turn_id: str
    verb: str
    args: dict[str, Any]
    ts: float = Field(default_factory=time.time)


class AgentStatusMsg(_Base):
    type: Literal["agent_status"] = "agent_status"
    state: Literal["idle", "listening", "thinking", "speaking"]


class TranscriptMsg(_Base):
    type: Literal["transcript"] = "transcript"
    role: Literal["user", "assistant"]
    text: str
    final: bool


class ErrorMsg(_Base):
    type: Literal["error"] = "error"
    code: str
    message: str


ServerMessage = Annotated[
    CommandMsg | AgentStatusMsg | TranscriptMsg | ErrorMsg,
    Field(discriminator="type"),
]


def parse_client_message(raw: str) -> ClientMessage:
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError("malformed", "message is not valid JSON") from exc
    if not isinstance(body, dict):
        raise ProtocolError("malformed", "message must be a JSON object")
    version = body.get("v")
    if version != PROTOCOL_VERSION:
        raise ProtocolError(
            "unsupported_version",
            f"protocol version {version!r} is not supported; expected {PROTOCOL_VERSION}",
        )
    try:
        return _client_adapter.validate_python(body)
    except ValidationError as exc:
        raise ProtocolError("invalid", str(exc)) from exc
