# TV Avatar Backend — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the media and control planes of the TV avatar backend — a Pipecat pipeline with SLNG STT/TTS and an Anam video avatar, plus a versioned WebSocket control protocol — with the agent/LLM layer deliberately stubbed.

**Architecture:** Two independent planes. The media plane is a Pipecat pipeline (`SmallWebRTC → SlngSTT → aggregator → screen-state injector → LLM → SlngTTS → AnamVideoService → out`). The control plane is an authenticated WebSocket carrying typed UI commands down and screen-state snapshots up. They meet only at an in-memory session store and a turn-scoped command bus. The LLM is a deterministic stub that satisfies Pipecat's LLM service interface, which makes frame-ordering and interruption tests reproducible.

**Tech Stack:** Python 3.11+, `uv`, Pipecat 1.11.x, `pipecat-slng` 0.5.2, `pipecat-anam` 0.2.0a6, FastAPI, Pydantic v2, pydantic-settings, pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-09-19-tv-avatar-backend-design.md`

## Global Constraints

- **Python ≥ 3.11** — floor set by both `pipecat-slng` 0.5.2 and `pipecat-anam` 0.2.0a6.
- **`uv` is the only dependency tool.** No `pip install` into the project env; every dependency change goes through `uv add` / `uv sync`, and `uv.lock` is committed.
- **`pipecat-anam==0.2.0a6` must be pinned exactly, with prereleases allowed for that package only.** The stable `0.1.0` targets the legacy `pipecat-ai>=0.0.103` line and will silently produce a broken environment.
- **No per-frame pixel work anywhere in the server.** The video path is decode → forward. (Spec §4, Frame rate.)
- **No secrets in source.** All keys via `pydantic-settings` from the environment; `.env` git-ignored, `.env.example` committed with blank values.
- **Every control-plane message carries `"v": 1`.** Unknown versions are rejected with an `error` message, never ignored.
- **`agent/commands.py` is the single source of truth** for command shapes. Anything else that needs those shapes — prompt text, TypeScript types, JSON Schema — is generated from it, never hand-written.
- **Fire-and-forget commands must never await the client.** Only `search_catalog` awaits, with a 400 ms timeout.

---

### Task 1: Project scaffold, pinned environment, and dependency proof

The riskiest thing in this project is the dependency combination (spec §13). This task exists to prove it before any code depends on it.

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `.env.example`, `src/tv_avatar/__init__.py`, `src/tv_avatar/config.py`
- Test: `tests/test_environment.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `tv_avatar.config.Settings` (pydantic-settings `BaseSettings`) with fields `slng_api_key: str`, `slng_base_url: str = "eu.api.slng.ai"`, `slng_stt_model: str`, `slng_tts_model: str`, `slng_tts_voice: str`, `anam_api_key: str`, `anam_avatar_id: str`, `anam_avatar_model: str = "cara-4"`, `video_width: int = 768`, `video_height: int = 1152`, `target_fps: int = 25`, `control_token_ttl_s: int = 3600`; and `tv_avatar.config.get_settings() -> Settings` (cached).

- [ ] **Step 1: Initialise the project with uv**

```bash
cd /Users/arnabdey0503/Documents/hackbarna_2026
uv init --package --name tv-avatar --python 3.11 .
```

If `uv init` refuses because the directory is non-empty, create `pyproject.toml` by hand in Step 2 instead and skip this step.

- [ ] **Step 2: Write `pyproject.toml` with exact pins**

```toml
[project]
name = "tv-avatar"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "pipecat-ai[silero,webrtc,runner]>=1.8.0,<2.0.0",
    "pipecat-slng==0.5.2",
    "pipecat-anam==0.2.0a6",
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "pydantic>=2.9",
    "pydantic-settings>=2.6",
    "loguru>=0.7",
]

[dependency-groups]
dev = [
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
    "httpx>=0.27",
]

[tool.uv]
# pipecat-anam's modern line is a prerelease; the stable 0.1.0 targets legacy Pipecat.
prerelease = "explicit"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/tv_avatar"]
```

- [ ] **Step 3: Write the failing environment proof test**

`tests/test_environment.py`:

```python
"""Proves the dependency combination that spec §13 flags as risky."""
import sys
from importlib.metadata import version


def test_python_is_at_least_311():
    assert sys.version_info >= (3, 11)


def test_pipecat_is_modern_line():
    major = int(version("pipecat-ai").split(".")[0])
    assert major >= 1, "pipecat-ai must be the 1.x line, not legacy 0.0.x"


def test_anam_plugin_is_the_prerelease_not_legacy_stable():
    # 0.1.0 (stable) depends on pipecat-ai>=0.0.103 and will not work here.
    assert version("pipecat-anam") == "0.2.0a6"


def test_plugins_import():
    from pipecat_anam import AnamVideoService
    from pipecat_slng import SlngSTTService, SlngTTSService

    assert AnamVideoService is not None
    assert SlngSTTService is not None
    assert SlngTTSService is not None
```

- [ ] **Step 4: Run it and watch it fail**

```bash
uv run pytest tests/test_environment.py -v
```

Expected: FAIL — packages not installed yet.

- [ ] **Step 5: Sync the environment**

```bash
uv sync
uv run pytest tests/test_environment.py -v
```

Expected: PASS. If `test_anam_plugin_is_the_prerelease_not_legacy_stable` fails reporting `0.1.0`, the prerelease pin did not take — confirm `prerelease = "explicit"` is under `[tool.uv]` and the dependency string is `==0.2.0a6`.

- [ ] **Step 6: Record the real Pipecat import paths**

Pipecat's module layout shifts between minor versions and later tasks depend on exact paths. Run this and save the output:

```bash
uv run python -c "
import pipecat, inspect, pkgutil
from importlib.metadata import version
print('pipecat-ai', version('pipecat-ai'))
from pipecat.services.llm_service import LLMService
print('LLMService OK:', LLMService)
print('abstract methods:', getattr(LLMService, '__abstractmethods__', None))
print(inspect.signature(LLMService.__init__))
"
```

Expected: prints the class and its abstract methods. **If the import raises `ModuleNotFoundError`, find the real path** with:

```bash
uv run python -c "
import pkgutil, pipecat.services
print([m.name for m in pkgutil.iter_modules(pipecat.services.__path__)])
"
```

Write the confirmed import path into a comment at the top of `src/tv_avatar/agent/llm.py` when you create it in Task 5. Task 5's stub subclasses this class, so getting it right here saves rework.

- [ ] **Step 7: Write `.gitignore`**

```gitignore
.env
.venv/
__pycache__/
*.py[cod]
.pytest_cache/
.serena/
.claude/.headroom_wrap_*
claude-setup.lock.json
```

- [ ] **Step 8: Write `.env.example` with blank values**

```dotenv
# SLNG — covers both STT and TTS
SLNG_API_KEY=
SLNG_BASE_URL=eu.api.slng.ai
SLNG_STT_MODEL=slng/deepgram/nova:3-en
SLNG_TTS_MODEL=slng/deepgram/aura:2-en
SLNG_TTS_VOICE=aura-2-thalia-en

# Anam
ANAM_API_KEY=
ANAM_AVATAR_ID=
ANAM_AVATAR_MODEL=cara-4
```

- [ ] **Step 9: Write the failing config test**

`tests/test_config.py`:

```python
import pytest
from pydantic import ValidationError

from tv_avatar.config import Settings


def test_settings_load_from_env(monkeypatch):
    monkeypatch.setenv("SLNG_API_KEY", "sk-test")
    monkeypatch.setenv("ANAM_API_KEY", "anam-test")
    monkeypatch.setenv("ANAM_AVATAR_ID", "avatar-1")
    s = Settings(_env_file=None)
    assert s.slng_api_key == "sk-test"
    assert s.slng_base_url == "eu.api.slng.ai"
    assert s.video_width == 768
    assert s.target_fps == 25


def test_missing_required_key_fails_fast(monkeypatch):
    monkeypatch.delenv("SLNG_API_KEY", raising=False)
    monkeypatch.delenv("ANAM_API_KEY", raising=False)
    monkeypatch.delenv("ANAM_AVATAR_ID", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
```

- [ ] **Step 10: Run it and watch it fail**

```bash
uv run pytest tests/test_config.py -v
```

Expected: FAIL — `ModuleNotFoundError: tv_avatar.config`.

- [ ] **Step 11: Write `src/tv_avatar/config.py`**

```python
"""Environment-backed configuration. No secret appears in source."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # SLNG — one key covers STT and TTS
    slng_api_key: str
    slng_base_url: str = "eu.api.slng.ai"
    slng_stt_model: str = "slng/deepgram/nova:3-en"
    slng_tts_model: str = "slng/deepgram/aura:2-en"
    slng_tts_voice: str = "aura-2-thalia-en"

    # Anam
    anam_api_key: str
    anam_avatar_id: str
    anam_avatar_model: str = "cara-4"

    # Media — cara-4 portrait; width and height must be supplied together
    video_width: int = 768
    video_height: int = 1152
    target_fps: int = 25

    control_token_ttl_s: int = 3600


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 12: Run both test files and verify they pass**

```bash
uv run pytest tests/ -v
```

Expected: PASS.

- [ ] **Step 13: Commit**

```bash
git add pyproject.toml uv.lock .gitignore .env.example src/ tests/
git commit -m "feat: scaffold tv-avatar with uv and pinned pipecat stack

Proves the pipecat-ai 1.x / pipecat-slng 0.5.2 / pipecat-anam 0.2.0a6
combination, and guards the prerelease pin with an explicit version
assertion so a silent downgrade to the legacy 0.1.0 line cannot pass."
```

---

### Task 2: Command schemas — the single source of truth

**Files:**
- Create: `src/tv_avatar/agent/__init__.py`, `src/tv_avatar/agent/commands.py`
- Test: `tests/test_commands.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Verb` (str enum); one Pydantic model per command (`Play`, `Pause`, `Resume`, `Seek`, `Navigate`, `Focus`, `OpenDetails`, `Close`, `Back`, `Home`, `ShowProducts`, `SearchCatalog`); `CommandArgs` (discriminated union on `verb`); `COMMAND_MODELS: dict[Verb, type[BaseModel]]`; `AWAITS_RESULT: frozenset[Verb]`; `parse_command(verb: str, args: dict) -> CommandArgs` raising `ValueError` on unknown verb or invalid args.

- [ ] **Step 1: Write the failing test**

`tests/test_commands.py`:

```python
import pytest

from tv_avatar.agent.commands import (
    AWAITS_RESULT,
    COMMAND_MODELS,
    Navigate,
    Play,
    Verb,
    parse_command,
)


def test_play_requires_title_id():
    cmd = parse_command("play", {"title_id": "tt_88"})
    assert isinstance(cmd, Play)
    assert cmd.title_id == "tt_88"


def test_unknown_verb_is_rejected():
    with pytest.raises(ValueError, match="unknown verb"):
        parse_command("self_destruct", {})


def test_invalid_args_are_rejected():
    with pytest.raises(ValueError):
        parse_command("play", {})  # missing title_id


def test_navigate_defaults_count_to_one():
    cmd = parse_command("navigate", {"direction": "right"})
    assert isinstance(cmd, Navigate)
    assert cmd.count == 1


def test_navigate_rejects_bad_direction():
    with pytest.raises(ValueError):
        parse_command("navigate", {"direction": "sideways"})


def test_seek_requires_exactly_one_of_to_or_delta():
    parse_command("seek", {"to_seconds": 30.0})
    parse_command("seek", {"delta_seconds": -10.0})
    with pytest.raises(ValueError):
        parse_command("seek", {})
    with pytest.raises(ValueError):
        parse_command("seek", {"to_seconds": 30.0, "delta_seconds": -10.0})


def test_every_verb_has_a_model():
    assert set(COMMAND_MODELS) == set(Verb)


def test_only_search_catalog_awaits_a_result():
    assert AWAITS_RESULT == frozenset({Verb.SEARCH_CATALOG})
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run pytest tests/test_commands.py -v
```

Expected: FAIL — `ModuleNotFoundError: tv_avatar.agent.commands`.

- [ ] **Step 3: Write `src/tv_avatar/agent/commands.py`**

```python
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
```

- [ ] **Step 4: Run tests and verify they pass**

```bash
uv run pytest tests/test_commands.py -v
```

Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add src/tv_avatar/agent/ tests/test_commands.py
git commit -m "feat: add command vocabulary as single source of truth"
```

---

### Task 3: Control protocol wire models

**Files:**
- Create: `src/tv_avatar/control/__init__.py`, `src/tv_avatar/control/protocol.py`
- Test: `tests/test_protocol.py`

**Interfaces:**
- Consumes: `tv_avatar.agent.commands.CommandArgs`, `Verb`.
- Produces: `PROTOCOL_VERSION: int = 1`; client→server models `ScreenStateMsg`, `AckMsg`, `ResultMsg`, `UserEventMsg` unioned as `ClientMessage`; server→client models `CommandMsg`, `AgentStatusMsg`, `TranscriptMsg`, `ErrorMsg` unioned as `ServerMessage`; supporting models `Tile`, `Playback`, `ScreenState`; `parse_client_message(raw: str) -> ClientMessage` raising `ProtocolError`; `ProtocolError(Exception)` with `.code`.

- [ ] **Step 1: Write the failing test**

`tests/test_protocol.py`:

```python
import json

import pytest

from tv_avatar.control.protocol import (
    PROTOCOL_VERSION,
    CommandMsg,
    ProtocolError,
    ScreenStateMsg,
    parse_client_message,
)


def _screen_state_payload(**over):
    payload = {
        "v": PROTOCOL_VERSION,
        "type": "screen_state",
        "state": {
            "view": "grid",
            "rail_id": "rail_trending",
            "focus_index": 1,
            "tiles": [
                {"title_id": "tt_1", "name": "Heat", "position": 0},
                {"title_id": "tt_2", "name": "Sicario", "position": 1},
            ],
            "playback": {"state": "stopped", "title_id": None, "position_s": 0.0},
        },
    }
    payload.update(over)
    return json.dumps(payload)


def test_parses_screen_state():
    msg = parse_client_message(_screen_state_payload())
    assert isinstance(msg, ScreenStateMsg)
    assert msg.state.focus_index == 1
    assert msg.state.tiles[1].name == "Sicario"


def test_rejects_unknown_protocol_version():
    with pytest.raises(ProtocolError) as exc:
        parse_client_message(_screen_state_payload(v=99))
    assert exc.value.code == "unsupported_version"


def test_rejects_malformed_json():
    with pytest.raises(ProtocolError) as exc:
        parse_client_message("{not json")
    assert exc.value.code == "malformed"


def test_rejects_unknown_message_type():
    with pytest.raises(ProtocolError) as exc:
        parse_client_message(json.dumps({"v": 1, "type": "launch_missiles"}))
    assert exc.value.code == "invalid"


def test_command_message_serialises_with_version():
    msg = CommandMsg(
        id="cmd_7",
        turn_id="turn_3",
        verb="play",
        args={"title_id": "tt_88"},
        ts=1758278400.123,
    )
    body = json.loads(msg.model_dump_json())
    assert body["v"] == PROTOCOL_VERSION
    assert body["type"] == "command"
    assert body["args"]["title_id"] == "tt_88"
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run pytest tests/test_protocol.py -v
```

Expected: FAIL — `ModuleNotFoundError: tv_avatar.control.protocol`.

- [ ] **Step 3: Write `src/tv_avatar/control/protocol.py`**

```python
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
```

- [ ] **Step 4: Run tests and verify they pass**

```bash
uv run pytest tests/test_protocol.py -v
```

Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/tv_avatar/control/ tests/test_protocol.py
git commit -m "feat: add versioned control protocol wire models"
```

---

### Task 4: Contract export for the TV repository

**Files:**
- Create: `tools/export_schemas.py`
- Test: `tests/test_export_schemas.py`

**Interfaces:**
- Consumes: `COMMAND_MODELS`, `ClientMessage`, `ServerMessage`, `PROTOCOL_VERSION`.
- Produces: `tools.export_schemas.build_schema_bundle() -> dict`; `tools.export_schemas.render_typescript(bundle: dict) -> str`; a `main()` writing `contracts/protocol.schema.json` and `contracts/protocol.d.ts`.

- [ ] **Step 1: Write the failing test**

`tests/test_export_schemas.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.export_schemas import build_schema_bundle, render_typescript  # noqa: E402
from tv_avatar.agent.commands import Verb  # noqa: E402


def test_bundle_covers_every_verb():
    bundle = build_schema_bundle()
    assert set(bundle["commands"]) == {v.value for v in Verb}


def test_bundle_records_protocol_version():
    assert build_schema_bundle()["protocol_version"] == 1


def test_typescript_declares_a_union_of_every_verb():
    ts = render_typescript(build_schema_bundle())
    assert "export type Verb =" in ts
    for verb in Verb:
        assert f'"{verb.value}"' in ts
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run pytest tests/test_export_schemas.py -v
```

Expected: FAIL — `ModuleNotFoundError: tools.export_schemas`.

- [ ] **Step 3: Write `tools/export_schemas.py`**

Create `tools/__init__.py` (empty) first, then:

```python
"""Generate the contract artifacts the TV repository consumes.

Run: uv run python tools/export_schemas.py
The TV app cannot compile a command this backend would reject, because
both sides derive from agent/commands.py.
"""
import json
from pathlib import Path

from pydantic import TypeAdapter

from tv_avatar.agent.commands import COMMAND_MODELS, AWAITS_RESULT, Verb
from tv_avatar.control.protocol import (
    PROTOCOL_VERSION,
    ClientMessage,
    ServerMessage,
)

OUT_DIR = Path(__file__).resolve().parents[1] / "contracts"


def build_schema_bundle() -> dict:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "awaits_result": sorted(v.value for v in AWAITS_RESULT),
        "commands": {
            verb.value: model.model_json_schema()
            for verb, model in COMMAND_MODELS.items()
        },
        "client_messages": TypeAdapter(ClientMessage).json_schema(),
        "server_messages": TypeAdapter(ServerMessage).json_schema(),
    }


def render_typescript(bundle: dict) -> str:
    verbs = " | ".join(f'"{v}"' for v in sorted(bundle["commands"]))
    awaits = " | ".join(f'"{v}"' for v in bundle["awaits_result"])
    return f"""// GENERATED by tools/export_schemas.py — do not edit by hand.
// Regenerate with: uv run python tools/export_schemas.py

export const PROTOCOL_VERSION = {bundle["protocol_version"]};

export type Verb = {verbs};

/** Verbs whose handler awaits a `result` message from the TV app. */
export type AwaitsResult = {awaits};

export interface Tile {{ title_id: string; name: string; position: number; }}

export interface Playback {{
  state: "stopped" | "playing" | "paused";
  title_id: string | null;
  position_s: number;
}}

export interface ScreenState {{
  view: "grid" | "details" | "player" | "products";
  rail_id: string | null;
  focus_index: number | null;
  tiles: Tile[];
  playback: Playback;
}}

export interface CommandMsg {{
  v: typeof PROTOCOL_VERSION;
  type: "command";
  id: string;
  turn_id: string;
  verb: Verb;
  args: Record<string, unknown>;
  ts: number;
}}

export interface AgentStatusMsg {{
  v: typeof PROTOCOL_VERSION;
  type: "agent_status";
  state: "idle" | "listening" | "thinking" | "speaking";
}}

export interface TranscriptMsg {{
  v: typeof PROTOCOL_VERSION;
  type: "transcript";
  role: "user" | "assistant";
  text: string;
  final: boolean;
}}

export interface ErrorMsg {{
  v: typeof PROTOCOL_VERSION;
  type: "error";
  code: string;
  message: string;
}}

export type ServerMessage = CommandMsg | AgentStatusMsg | TranscriptMsg | ErrorMsg;
"""


def main() -> None:
    bundle = build_schema_bundle()
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "protocol.schema.json").write_text(
        json.dumps(bundle, indent=2, sort_keys=True) + "\n"
    )
    (OUT_DIR / "protocol.d.ts").write_text(render_typescript(bundle))
    print(f"wrote {OUT_DIR}/protocol.schema.json and protocol.d.ts")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests and verify they pass**

```bash
uv run pytest tests/test_export_schemas.py -v
```

Expected: PASS (3 tests).

- [ ] **Step 5: Generate the artifacts and commit them**

```bash
uv run python tools/export_schemas.py
git add tools/ contracts/ tests/test_export_schemas.py
git commit -m "feat: generate JSON Schema and TypeScript contracts for the TV app"
```

---

### Task 5: Session and screen state store

**Files:**
- Create: `src/tv_avatar/session/__init__.py`, `src/tv_avatar/session/state.py`
- Test: `tests/test_session_state.py`

**Interfaces:**
- Consumes: `tv_avatar.control.protocol.ScreenState`.
- Produces: `SessionState` dataclass with `session_id: str`, `control_token: str`, `created_at: float`, `screen: ScreenState | None`, `current_turn_id: str | None`, methods `update_screen(state: ScreenState) -> None`, `render_for_prompt() -> str`, `new_turn() -> str`; `SessionStore` with `create(ttl_s: int) -> SessionState`, `get(session_id: str) -> SessionState | None`, `authenticate(session_id: str, token: str) -> SessionState` raising `KeyError`/`PermissionError`, `close(session_id: str) -> None`.

- [ ] **Step 1: Write the failing test**

`tests/test_session_state.py`:

```python
import pytest

from tv_avatar.control.protocol import Playback, ScreenState, Tile
from tv_avatar.session.state import SessionStore


def _state():
    return ScreenState(
        view="grid",
        rail_id="rail_trending",
        focus_index=1,
        tiles=[
            Tile(title_id="tt_1", name="Heat", position=0),
            Tile(title_id="tt_2", name="Sicario", position=1),
        ],
        playback=Playback(state="stopped"),
    )


def test_create_returns_distinct_ids_and_tokens():
    store = SessionStore()
    a, b = store.create(60), store.create(60)
    assert a.session_id != b.session_id
    assert a.control_token != b.control_token


def test_authenticate_accepts_matching_token():
    store = SessionStore()
    s = store.create(60)
    assert store.authenticate(s.session_id, s.control_token) is s


def test_authenticate_rejects_wrong_token():
    store = SessionStore()
    s = store.create(60)
    with pytest.raises(PermissionError):
        store.authenticate(s.session_id, "not-the-token")


def test_authenticate_rejects_unknown_session():
    with pytest.raises(KeyError):
        SessionStore().authenticate("nope", "tok")


def test_latest_screen_state_wins():
    store = SessionStore()
    s = store.create(60)
    s.update_screen(_state())
    later = _state()
    later.focus_index = 0
    s.update_screen(later)
    assert s.screen.focus_index == 0


def test_render_for_prompt_mentions_focused_tile():
    store = SessionStore()
    s = store.create(60)
    s.update_screen(_state())
    rendered = s.render_for_prompt()
    assert "Sicario" in rendered
    assert "focused" in rendered.lower()


def test_render_for_prompt_handles_no_state_yet():
    s = SessionStore().create(60)
    assert "unknown" in s.render_for_prompt().lower()


def test_new_turn_changes_the_turn_id():
    s = SessionStore().create(60)
    first = s.new_turn()
    assert s.current_turn_id == first
    assert s.new_turn() != first


def test_close_removes_the_session():
    store = SessionStore()
    s = store.create(60)
    store.close(s.session_id)
    assert store.get(s.session_id) is None
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run pytest tests/test_session_state.py -v
```

Expected: FAIL — `ModuleNotFoundError: tv_avatar.session.state`.

- [ ] **Step 3: Write `src/tv_avatar/session/state.py`**

```python
"""In-memory session and screen-state store.

Screen state is pushed by the TV app (spec §D2): latest wins, no history.
A dict is deliberate for phase 1 — swapping in Redis later touches only
SessionStore, not its callers.
"""
import secrets
import time
import uuid
from dataclasses import dataclass, field

from tv_avatar.control.protocol import ScreenState


@dataclass
class SessionState:
    session_id: str
    control_token: str
    expires_at: float
    created_at: float = field(default_factory=time.time)
    screen: ScreenState | None = None
    current_turn_id: str | None = None

    def update_screen(self, state: ScreenState) -> None:
        self.screen = state

    def new_turn(self) -> str:
        self.current_turn_id = f"turn_{uuid.uuid4().hex[:8]}"
        return self.current_turn_id

    def render_for_prompt(self) -> str:
        """Compact rendering injected fresh into every LLM run (spec §4)."""
        if self.screen is None:
            return "Screen state: unknown (the TV app has not reported yet)."
        s = self.screen
        lines = [f"View: {s.view}"]
        if s.rail_id:
            lines.append(f"Rail: {s.rail_id}")
        for tile in s.tiles:
            marker = " <- focused" if tile.position == s.focus_index else ""
            lines.append(f"  [{tile.position}] {tile.name} (id={tile.title_id}){marker}")
        pb = s.playback
        if pb.state == "stopped":
            lines.append("Playback: stopped")
        else:
            lines.append(
                f"Playback: {pb.state} {pb.title_id} at {pb.position_s:.0f}s"
            )
        return "\n".join(lines)


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}

    def create(self, ttl_s: int) -> SessionState:
        session = SessionState(
            session_id=f"sess_{uuid.uuid4().hex[:12]}",
            control_token=secrets.token_urlsafe(32),
            expires_at=time.time() + ttl_s,
        )
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> SessionState | None:
        return self._sessions.get(session_id)

    def authenticate(self, session_id: str, token: str) -> SessionState:
        """The session id travels in a URL across app boundaries, so it is not
        a credential. The token is (spec §7, Control channel authentication)."""
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        if not secrets.compare_digest(session.control_token, token):
            raise PermissionError("invalid control token")
        if time.time() > session.expires_at:
            raise PermissionError("control token expired")
        return session

    def close(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
```

- [ ] **Step 4: Run tests and verify they pass**

```bash
uv run pytest tests/test_session_state.py -v
```

Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add src/tv_avatar/session/ tests/test_session_state.py
git commit -m "feat: add session store with token auth and screen state"
```

---

### Task 6: Turn-scoped command bus

**Files:**
- Create: `src/tv_avatar/control/bus.py`
- Test: `tests/test_command_bus.py`

**Interfaces:**
- Consumes: `tv_avatar.agent.commands.{parse_command, AWAITS_RESULT, Verb}`, `tv_avatar.control.protocol.CommandMsg`.
- Produces: `CommandBus(search_timeout_s: float = 0.4)` with `async dispatch(verb: str, args: dict, turn_id: str) -> dict`, `async next_outbound() -> CommandMsg`, `cancel_turn(turn_id: str) -> int`, `resolve(command_id: str, data: dict) -> None`, `pending_count() -> int`.

- [ ] **Step 1: Write the failing test**

`tests/test_command_bus.py`:

```python
import asyncio

import pytest

from tv_avatar.control.bus import CommandBus


async def test_fire_and_forget_returns_immediately():
    bus = CommandBus()
    result = await asyncio.wait_for(
        bus.dispatch("pause", {}, turn_id="turn_1"), timeout=0.1
    )
    assert result == {"status": "dispatched"}


async def test_dispatched_command_appears_on_the_outbound_queue():
    bus = CommandBus()
    await bus.dispatch("play", {"title_id": "tt_9"}, turn_id="turn_1")
    msg = await asyncio.wait_for(bus.next_outbound(), timeout=0.1)
    assert msg.verb == "play"
    assert msg.args["title_id"] == "tt_9"
    assert msg.turn_id == "turn_1"


async def test_invalid_command_never_reaches_the_queue():
    bus = CommandBus()
    with pytest.raises(ValueError):
        await bus.dispatch("play", {}, turn_id="turn_1")
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(bus.next_outbound(), timeout=0.05)


async def test_cancel_turn_drops_unsent_commands_for_that_turn_only():
    bus = CommandBus()
    await bus.dispatch("pause", {}, turn_id="turn_1")
    await bus.dispatch("home", {}, turn_id="turn_1")
    await bus.dispatch("back", {}, turn_id="turn_2")
    dropped = bus.cancel_turn("turn_1")
    assert dropped == 2
    msg = await asyncio.wait_for(bus.next_outbound(), timeout=0.1)
    assert msg.verb == "back"


async def test_search_catalog_awaits_and_resolves():
    bus = CommandBus()
    task = asyncio.create_task(
        bus.dispatch("search_catalog", {"query": "tom hanks"}, turn_id="turn_1")
    )
    msg = await asyncio.wait_for(bus.next_outbound(), timeout=0.1)
    bus.resolve(msg.id, {"titles": [{"title_id": "tt_5", "name": "Big"}]})
    result = await asyncio.wait_for(task, timeout=0.1)
    assert result["titles"][0]["name"] == "Big"


async def test_search_catalog_degrades_on_timeout():
    bus = CommandBus(search_timeout_s=0.05)
    result = await asyncio.wait_for(
        bus.dispatch("search_catalog", {"query": "x"}, turn_id="turn_1"), timeout=0.5
    )
    assert result["status"] == "unavailable"
    assert bus.pending_count() == 0
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run pytest tests/test_command_bus.py -v
```

Expected: FAIL — `ModuleNotFoundError: tv_avatar.control.bus`.

- [ ] **Step 3: Write `src/tv_avatar/control/bus.py`**

```python
"""Bridge between the agent's tool handlers and the control WebSocket.

Two rules from spec §8 and §9 live here:
  - fire-and-forget commands never await the client, because a slow TV
    would stall the LLM turn and stall speech with it;
  - the queue is turn-scoped, so barge-in drops commands the interrupted
    turn had queued but not yet sent.
"""
import asyncio
import uuid
from collections import deque

from tv_avatar.agent.commands import AWAITS_RESULT, Verb, parse_command
from tv_avatar.control.protocol import CommandMsg


class CommandBus:
    def __init__(self, search_timeout_s: float = 0.4) -> None:
        self._outbound: deque[CommandMsg] = deque()
        self._ready = asyncio.Event()
        self._pending: dict[str, asyncio.Future[dict]] = {}
        self._search_timeout_s = search_timeout_s

    async def dispatch(self, verb: str, args: dict, turn_id: str) -> dict:
        command = parse_command(verb, args)  # raises ValueError; never queued
        msg = CommandMsg(
            id=f"cmd_{uuid.uuid4().hex[:8]}",
            turn_id=turn_id,
            verb=command.verb.value,
            args=command.model_dump(exclude={"verb"}, exclude_none=True),
        )

        if command.verb not in AWAITS_RESULT:
            self._enqueue(msg)
            return {"status": "dispatched"}

        future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[msg.id] = future
        self._enqueue(msg)
        try:
            return await asyncio.wait_for(future, timeout=self._search_timeout_s)
        except asyncio.TimeoutError:
            return {"status": "unavailable", "reason": "timeout"}
        finally:
            self._pending.pop(msg.id, None)

    def _enqueue(self, msg: CommandMsg) -> None:
        self._outbound.append(msg)
        self._ready.set()

    async def next_outbound(self) -> CommandMsg:
        while not self._outbound:
            self._ready.clear()
            await self._ready.wait()
        return self._outbound.popleft()

    def cancel_turn(self, turn_id: str) -> int:
        """Drop queued-but-unsent commands for an interrupted turn.

        Commands already handed to the WebSocket are NOT rolled back
        (spec §9, rule 4)."""
        keep = deque(m for m in self._outbound if m.turn_id != turn_id)
        dropped = len(self._outbound) - len(keep)
        self._outbound = keep
        if not self._outbound:
            self._ready.clear()
        return dropped

    def resolve(self, command_id: str, data: dict) -> None:
        future = self._pending.get(command_id)
        if future is not None and not future.done():
            future.set_result(data)

    def pending_count(self) -> int:
        return len(self._pending)
```

- [ ] **Step 4: Run tests and verify they pass**

```bash
uv run pytest tests/test_command_bus.py -v
```

Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/tv_avatar/control/bus.py tests/test_command_bus.py
git commit -m "feat: add turn-scoped command bus with search correlation"
```

---

### Task 7: Placeholder LLM service

The real agent is deferred (spec §14). This stub satisfies the Pipecat LLM interface with deterministic output, which is what makes the interruption and frame-ordering tests in Tasks 9–10 reproducible. It stays afterwards as the pipeline's test double.

**Files:**
- Create: `src/tv_avatar/agent/llm.py`
- Test: `tests/test_stub_llm.py`

**Interfaces:**
- Consumes: `tv_avatar.control.bus.CommandBus`.
- Produces: `ScriptedTurn(text: str, commands: list[tuple[str, dict]])`; `StubLLMService(bus: CommandBus, script: list[ScriptedTurn] | None = None)` — a Pipecat `LLMService` subclass; `DEFAULT_SCRIPT: list[ScriptedTurn]`.

- [ ] **Step 1: Confirm the base class path from Task 1 Step 6**

```bash
uv run python -c "from pipecat.services.llm_service import LLMService; print(LLMService.__abstractmethods__)"
```

Use whatever path Task 1 Step 6 confirmed. If `process_frame` is the only abstract method, the implementation below is correct as written; if the installed version names it differently, adjust the override name and keep the body.

- [ ] **Step 2: Write the failing test**

`tests/test_stub_llm.py`:

```python
from tv_avatar.agent.llm import DEFAULT_SCRIPT, ScriptedTurn, StubLLMService
from tv_avatar.control.bus import CommandBus


def test_default_script_is_non_empty_and_well_formed():
    assert DEFAULT_SCRIPT
    for turn in DEFAULT_SCRIPT:
        assert turn.text
        for verb, args in turn.commands:
            assert isinstance(verb, str)
            assert isinstance(args, dict)


def test_script_advances_and_wraps():
    script = [
        ScriptedTurn("first", []),
        ScriptedTurn("second", [("pause", {})]),
    ]
    svc = StubLLMService(bus=CommandBus(), script=script)
    assert svc.next_turn().text == "first"
    assert svc.next_turn().text == "second"
    assert svc.next_turn().text == "first"


async def test_running_a_turn_dispatches_its_commands():
    bus = CommandBus()
    svc = StubLLMService(
        bus=bus, script=[ScriptedTurn("ok", [("play", {"title_id": "tt_3"})])]
    )
    text = await svc.run_scripted_turn(turn_id="turn_1")
    assert text == "ok"
    msg = await bus.next_outbound()
    assert msg.verb == "play"
    assert msg.args["title_id"] == "tt_3"
```

- [ ] **Step 3: Run it and watch it fail**

```bash
uv run pytest tests/test_stub_llm.py -v
```

Expected: FAIL — `ModuleNotFoundError: tv_avatar.agent.llm`.

- [ ] **Step 4: Write `src/tv_avatar/agent/llm.py`**

```python
"""Placeholder agent. The real one is deferred (spec §14).

Deterministic by design: a scripted stub makes interruption and
frame-ordering assertions reproducible in a way a real model cannot.
This module keeps its value after phase 2 as the pipeline test double.

Base class path confirmed in Task 1, Step 6.
"""
from dataclasses import dataclass, field

from pipecat.services.llm_service import LLMService

from tv_avatar.control.bus import CommandBus


@dataclass
class ScriptedTurn:
    text: str
    commands: list[tuple[str, dict]] = field(default_factory=list)


DEFAULT_SCRIPT: list[ScriptedTurn] = [
    ScriptedTurn("Sure, putting that on now.", [("play", {"title_id": "tt_1"})]),
    ScriptedTurn("Paused.", [("pause", {})]),
    ScriptedTurn("Moving right.", [("navigate", {"direction": "right", "count": 1})]),
    ScriptedTurn("Here's what's on screen.", []),
]


class StubLLMService(LLMService):
    """Emits fixed prose and fixed commands, ignoring its input entirely."""

    def __init__(
        self,
        bus: CommandBus,
        script: list[ScriptedTurn] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._bus = bus
        self._script = script or DEFAULT_SCRIPT
        self._index = 0

    def next_turn(self) -> ScriptedTurn:
        turn = self._script[self._index % len(self._script)]
        self._index += 1
        return turn

    async def run_scripted_turn(self, turn_id: str) -> str:
        """Dispatch this turn's commands, then return its prose.

        Commands go first so the UI reacts before speech completes, which
        is the ordering the real agent must also honour (spec §5).
        """
        turn = self.next_turn()
        for verb, args in turn.commands:
            await self._bus.dispatch(verb, args, turn_id=turn_id)
        return turn.text
```

- [ ] **Step 5: Run tests and verify they pass**

```bash
uv run pytest tests/test_stub_llm.py -v
```

Expected: PASS (3 tests). If `LLMService.__init__` rejects the bare `**kwargs`, inspect its signature from Task 1 Step 6 and pass the required arguments explicitly.

- [ ] **Step 6: Commit**

```bash
git add src/tv_avatar/agent/llm.py tests/test_stub_llm.py
git commit -m "feat: add deterministic placeholder LLM service"
```

---

### Task 8: FastAPI session lifecycle and control WebSocket

**Files:**
- Create: `src/tv_avatar/control/channel.py`, `src/tv_avatar/session/manager.py`, `src/tv_avatar/app.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `SessionStore`, `CommandBus`, `parse_client_message`, `ProtocolError`, `ErrorMsg`, `get_settings`.
- Produces: `create_app(store: SessionStore | None = None) -> FastAPI` exposing `POST /sessions` → `{"session_id", "control_token", "control_url", "protocol_version"}` and `WS /sessions/{session_id}/control?token=...`; `SessionManager` holding one `CommandBus` per session with `bus_for(session_id) -> CommandBus` and `drop(session_id) -> None`; `ControlChannel.run(websocket, session, bus)`.

- [ ] **Step 1: Write the failing test**

`tests/test_app.py`:

```python
import json

from fastapi.testclient import TestClient

from tv_avatar.app import create_app
from tv_avatar.control.protocol import PROTOCOL_VERSION


def _client():
    return TestClient(create_app())


def test_create_session_returns_token_and_url():
    with _client() as c:
        body = c.post("/sessions").json()
        assert body["session_id"].startswith("sess_")
        assert len(body["control_token"]) > 20
        assert body["session_id"] in body["control_url"]
        assert body["protocol_version"] == PROTOCOL_VERSION


def test_control_socket_rejects_missing_token():
    with _client() as c:
        sid = c.post("/sessions").json()["session_id"]
        with c.websocket_connect(f"/sessions/{sid}/control") as ws:
            msg = ws.receive_json()
            assert msg["type"] == "error"
            assert msg["code"] == "unauthorized"


def test_control_socket_rejects_wrong_token():
    with _client() as c:
        sid = c.post("/sessions").json()["session_id"]
        with c.websocket_connect(f"/sessions/{sid}/control?token=wrong") as ws:
            assert ws.receive_json()["code"] == "unauthorized"


def test_control_socket_accepts_valid_token_and_stores_screen_state():
    with _client() as c:
        body = c.post("/sessions").json()
        url = f"/sessions/{body['session_id']}/control?token={body['control_token']}"
        with c.websocket_connect(url) as ws:
            assert ws.receive_json()["type"] == "agent_status"
            ws.send_text(json.dumps({
                "v": PROTOCOL_VERSION,
                "type": "screen_state",
                "state": {
                    "view": "grid",
                    "rail_id": "r1",
                    "focus_index": 0,
                    "tiles": [{"title_id": "tt_1", "name": "Heat", "position": 0}],
                    "playback": {"state": "stopped", "title_id": None,
                                 "position_s": 0.0},
                },
            }))
            ws.send_text(json.dumps({"v": 99, "type": "screen_state"}))
            err = ws.receive_json()
            assert err["code"] == "unsupported_version"


def test_dispatched_command_is_delivered_over_the_socket():
    app = create_app()
    with TestClient(app) as c:
        body = c.post("/sessions").json()
        bus = app.state.manager.bus_for(body["session_id"])
        url = f"/sessions/{body['session_id']}/control?token={body['control_token']}"
        with c.websocket_connect(url) as ws:
            ws.receive_json()  # agent_status
            import anyio
            anyio.from_thread.run_sync(lambda: None)  # yield to the loop
            c.portal.call(bus.dispatch, "home", {}, "turn_1")
            msg = ws.receive_json()
            assert msg["type"] == "command"
            assert msg["verb"] == "home"
```

If `c.portal` is unavailable in the installed Starlette version, replace that last test's dispatch line with a direct `asyncio.run_coroutine_threadsafe(...)` against the app's loop, or move the assertion into Task 10's integration test — do not delete the coverage.

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run pytest tests/test_app.py -v
```

Expected: FAIL — `ModuleNotFoundError: tv_avatar.app`.

- [ ] **Step 3: Write `src/tv_avatar/session/manager.py`**

```python
"""One command bus per session; the pipeline and the socket share it."""
from tv_avatar.control.bus import CommandBus


class SessionManager:
    def __init__(self) -> None:
        self._buses: dict[str, CommandBus] = {}

    def bus_for(self, session_id: str) -> CommandBus:
        return self._buses.setdefault(session_id, CommandBus())

    def drop(self, session_id: str) -> None:
        self._buses.pop(session_id, None)
```

- [ ] **Step 4: Write `src/tv_avatar/control/channel.py`**

```python
"""Control WebSocket: commands down, screen state up.

Reader and writer run concurrently so a silent client never blocks
command delivery, and a chatty client never delays it.
"""
import asyncio

from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger

from tv_avatar.control.bus import CommandBus
from tv_avatar.control.protocol import (
    AgentStatusMsg,
    ErrorMsg,
    ProtocolError,
    parse_client_message,
)
from tv_avatar.session.state import SessionState


class ControlChannel:
    def __init__(self, websocket: WebSocket, session: SessionState, bus: CommandBus):
        self._ws = websocket
        self._session = session
        self._bus = bus

    async def run(self) -> None:
        await self._ws.send_text(AgentStatusMsg(state="idle").model_dump_json())
        reader = asyncio.create_task(self._read_loop())
        writer = asyncio.create_task(self._write_loop())
        done, pending = await asyncio.wait(
            {reader, writer}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc and not isinstance(exc, WebSocketDisconnect):
                logger.warning("control channel ended: {}", exc)

    async def _read_loop(self) -> None:
        while True:
            raw = await self._ws.receive_text()
            try:
                msg = parse_client_message(raw)
            except ProtocolError as err:
                await self._ws.send_text(
                    ErrorMsg(code=err.code, message=err.message).model_dump_json()
                )
                continue
            await self._handle(msg)

    async def _handle(self, msg) -> None:
        match msg.type:
            case "screen_state":
                self._session.update_screen(msg.state)
            case "result":
                self._bus.resolve(msg.command_id, msg.data)
            case "ack":
                if not msg.ok:
                    logger.warning("command {} failed: {}", msg.command_id, msg.error)
            case "user_event":
                logger.info("user event {}: {}", msg.event, msg.detail)

    async def _write_loop(self) -> None:
        while True:
            command = await self._bus.next_outbound()
            await self._ws.send_text(command.model_dump_json())
```

- [ ] **Step 5: Write `src/tv_avatar/app.py`**

```python
"""HTTP session lifecycle and the control WebSocket endpoint."""
from fastapi import FastAPI, Query, WebSocket

from tv_avatar.config import get_settings
from tv_avatar.control.channel import ControlChannel
from tv_avatar.control.protocol import PROTOCOL_VERSION, ErrorMsg
from tv_avatar.session.manager import SessionManager
from tv_avatar.session.state import SessionStore


def create_app(store: SessionStore | None = None) -> FastAPI:
    app = FastAPI(title="tv-avatar")
    app.state.store = store or SessionStore()
    app.state.manager = SessionManager()

    @app.post("/sessions")
    async def create_session() -> dict:
        ttl = get_settings().control_token_ttl_s if _settings_available() else 3600
        session = app.state.store.create(ttl)
        app.state.manager.bus_for(session.session_id)
        return {
            "session_id": session.session_id,
            "control_token": session.control_token,
            "control_url": f"/sessions/{session.session_id}/control",
            "protocol_version": PROTOCOL_VERSION,
        }

    @app.websocket("/sessions/{session_id}/control")
    async def control(websocket: WebSocket, session_id: str,
                      token: str = Query(default="")) -> None:
        await websocket.accept()
        try:
            session = app.state.store.authenticate(session_id, token)
        except (KeyError, PermissionError):
            # Deliberately indistinguishable: an unknown session and a bad
            # token leak different information if reported separately.
            await websocket.send_text(
                ErrorMsg(code="unauthorized",
                         message="invalid session or token").model_dump_json()
            )
            await websocket.close(code=4401)
            return
        bus = app.state.manager.bus_for(session_id)
        await ControlChannel(websocket, session, bus).run()

    return app


def _settings_available() -> bool:
    try:
        get_settings()
        return True
    except Exception:  # noqa: BLE001 — tests run without keys present
        return False


app = create_app()
```

- [ ] **Step 6: Run tests and verify they pass**

```bash
uv run pytest tests/test_app.py -v
```

Expected: PASS (5 tests).

- [ ] **Step 7: Commit**

```bash
git add src/tv_avatar/app.py src/tv_avatar/control/channel.py src/tv_avatar/session/manager.py tests/test_app.py
git commit -m "feat: add session endpoint and authenticated control websocket"
```

---

### Task 9: Pipeline builder — M0, voice loop without the avatar

**Files:**
- Create: `src/tv_avatar/pipeline/__init__.py`, `src/tv_avatar/pipeline/services.py`, `src/tv_avatar/pipeline/builder.py`
- Test: `tests/test_pipeline_builder.py`

**Interfaces:**
- Consumes: `get_settings`, `StubLLMService`, `CommandBus`, `SessionState`.
- Produces: `build_stt(settings) -> SlngSTTService`, `build_tts(settings) -> SlngTTSService`, `build_anam(settings) -> AnamVideoService`; `build_pipeline(transport, session, bus, *, with_avatar: bool = True) -> PipelineTask`.

- [ ] **Step 1: Write the failing test**

`tests/test_pipeline_builder.py`:

```python
from tv_avatar.config import Settings
from tv_avatar.pipeline.services import build_anam, build_stt, build_tts


def _settings():
    return Settings(
        slng_api_key="sk-test",
        anam_api_key="anam-test",
        anam_avatar_id="avatar-1",
        _env_file=None,
    )


def test_stt_uses_configured_model_and_region():
    stt = build_stt(_settings())
    assert stt is not None


def test_tts_uses_configured_voice():
    tts = build_tts(_settings())
    assert tts is not None


def test_anam_enables_audio_passthrough_and_disables_replay():
    # Passthrough means our TTS drives the avatar rather than Anam's own
    # voice; replay off keeps Anam from recording the session (spec §4).
    anam = build_anam(_settings())
    assert anam is not None
```

These are construction smoke tests: they prove the constructor signatures in the installed plugin versions match what `services.py` passes. That is precisely the M0 risk.

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run pytest tests/test_pipeline_builder.py -v
```

Expected: FAIL — `ModuleNotFoundError: tv_avatar.pipeline.services`.

- [ ] **Step 3: Write `src/tv_avatar/pipeline/services.py`**

```python
"""Construct the third-party services from configuration.

Voice, speed and language are pinned for the session: changing any of
them mid-session forces a SLNG WebSocket reconnect, heard as a gap.
"""
from anam import PersonaConfig
from pipecat.transcriptions.language import Language
from pipecat_anam import AnamVideoService
from pipecat_slng import SlngSTTService, SlngTTSService

from tv_avatar.config import Settings


def build_stt(settings: Settings) -> SlngSTTService:
    return SlngSTTService(
        api_key=settings.slng_api_key,
        model=settings.slng_stt_model,
        base_url=settings.slng_base_url,
        language=Language.EN,
        enable_vad=True,
        enable_partials=True,
    )


def build_tts(settings: Settings) -> SlngTTSService:
    return SlngTTSService(
        api_key=settings.slng_api_key,
        model=settings.slng_tts_model,
        voice=settings.slng_tts_voice,
        base_url=settings.slng_base_url,
        language=Language.EN,
    )


def build_anam(settings: Settings) -> AnamVideoService:
    return AnamVideoService(
        api_key=settings.anam_api_key,
        persona_config=PersonaConfig(
            avatar_id=settings.anam_avatar_id,
            avatar_model=settings.anam_avatar_model,
            enable_audio_passthrough=True,
            enable_session_replay=False,
        ),
        video_width=settings.video_width,
        video_height=settings.video_height,
    )
```

- [ ] **Step 4: Run the smoke tests**

```bash
uv run pytest tests/test_pipeline_builder.py -v
```

Expected: PASS. **If any constructor raises `TypeError` on an unexpected keyword**, the installed plugin version differs from the documented one — inspect the real signature and correct `services.py`:

```bash
uv run python -c "
import inspect
from pipecat_slng import SlngSTTService, SlngTTSService
from pipecat_anam import AnamVideoService
for c in (SlngSTTService, SlngTTSService, AnamVideoService):
    print(c.__name__, inspect.signature(c.__init__))
"
```

- [ ] **Step 5: Write `src/tv_avatar/pipeline/builder.py`**

```python
"""Assemble the Pipecat pipeline (spec §4).

Only two elements are ours: the screen-state injector and the agent.
Everything else is library code wired from configuration.
"""
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)

from tv_avatar.agent.llm import StubLLMService
from tv_avatar.config import get_settings
from tv_avatar.control.bus import CommandBus
from tv_avatar.pipeline.services import build_anam, build_stt, build_tts
from tv_avatar.session.state import SessionState


def build_pipeline(
    transport,
    session: SessionState,
    bus: CommandBus,
    *,
    with_avatar: bool = True,
) -> PipelineTask:
    settings = get_settings()
    context = LLMContext()
    user_agg, assistant_agg = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    stages = [
        transport.input(),
        build_stt(settings),
        user_agg,
        StubLLMService(bus=bus),
        build_tts(settings),
    ]
    if with_avatar:
        stages.append(build_anam(settings))
    stages += [transport.output(), assistant_agg]

    return PipelineTask(Pipeline(stages), params=PipelineParams(enable_metrics=True))
```

- [ ] **Step 6: Run the full suite**

```bash
uv run pytest tests/ -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/tv_avatar/pipeline/ tests/test_pipeline_builder.py
git commit -m "feat: add pipeline builder with SLNG services and Anam avatar"
```

---

### Task 10: Mock TV client

**Files:**
- Create: `tools/mock_tv_client/index.html`, `tools/mock_tv_client/client.js`
- Modify: `src/tv_avatar/app.py` (mount static files)

**Interfaces:**
- Consumes: `POST /sessions`, `WS /sessions/{id}/control`, `contracts/protocol.d.ts` as the shape reference.
- Produces: a page that opens a session, connects the control socket, renders a fake 2×4 grid, applies every command verb, and pushes `screen_state` after each change.

- [ ] **Step 1: Write `tools/mock_tv_client/index.html`**

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Mock TV client</title>
  <style>
    body { background:#111; color:#eee; font:14px system-ui; margin:0; padding:24px; }
    #grid { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; max-width:800px; }
    .tile { background:#222; border:2px solid transparent; border-radius:8px;
            padding:24px 12px; text-align:center; }
    .tile.focused { border-color:#4ea1ff; }
    #log { margin-top:24px; height:240px; overflow:auto; background:#000;
           padding:12px; border-radius:8px; white-space:pre-wrap; }
    #status { color:#4ea1ff; }
  </style>
</head>
<body>
  <h1>Mock TV client</h1>
  <p>Agent: <span id="status">disconnected</span> · Playback: <span id="playback">stopped</span></p>
  <div id="grid"></div>
  <div id="log"></div>
  <script src="client.js"></script>
</body>
</html>
```

- [ ] **Step 2: Write `tools/mock_tv_client/client.js`**

```javascript
// Mock TV app. Mirrors contracts/protocol.d.ts — regenerate that file
// with `uv run python tools/export_schemas.py` after any command change.
const V = 1;
const TITLES = [
  { title_id: "tt_1", name: "Heat" },
  { title_id: "tt_2", name: "Sicario" },
  { title_id: "tt_3", name: "Arrival" },
  { title_id: "tt_4", name: "Dune" },
];

let ws = null;
let focus = 0;
let playback = { state: "stopped", title_id: null, position_s: 0 };

const log = (m) => {
  const el = document.getElementById("log");
  el.textContent += m + "\n";
  el.scrollTop = el.scrollHeight;
};

function render() {
  document.getElementById("grid").innerHTML = TITLES.map(
    (t, i) =>
      `<div class="tile ${i === focus ? "focused" : ""}">${t.name}</div>`
  ).join("");
  document.getElementById("playback").textContent =
    playback.state === "stopped" ? "stopped" : `${playback.state} ${playback.title_id}`;
}

function pushScreenState() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({
    v: V,
    type: "screen_state",
    state: {
      view: playback.state === "playing" ? "player" : "grid",
      rail_id: "rail_mock",
      focus_index: focus,
      tiles: TITLES.map((t, i) => ({ ...t, position: i })),
      playback,
    },
  }));
}

function ack(id, ok = true, error = null) {
  ws.send(JSON.stringify({ v: V, type: "ack", command_id: id, ok, error }));
}

function apply(msg) {
  const a = msg.args || {};
  switch (msg.verb) {
    case "navigate":
      focus = Math.min(TITLES.length - 1, Math.max(0,
        focus + (a.direction === "right" ? (a.count || 1)
              : a.direction === "left" ? -(a.count || 1) : 0)));
      break;
    case "focus":
      focus = Math.max(0, TITLES.findIndex((t) => t.title_id === a.title_id));
      break;
    case "play":
      playback = { state: "playing", title_id: a.title_id, position_s: a.resume_from || 0 };
      break;
    case "pause": playback.state = "paused"; break;
    case "resume": playback.state = "playing"; break;
    case "seek":
      playback.position_s = a.to_seconds ?? (playback.position_s + (a.delta_seconds || 0));
      break;
    case "back": case "home": case "close":
      playback = { state: "stopped", title_id: null, position_s: 0 };
      break;
    case "search_catalog":
      ws.send(JSON.stringify({ v: V, type: "result", command_id: msg.id,
        data: { titles: TITLES.slice(0, a.limit || 10) } }));
      break;
    default:
      log(`unhandled verb: ${msg.verb}`);
      ack(msg.id, false, "unhandled verb");
      return;
  }
  ack(msg.id);
  render();
  pushScreenState();
}

async function start() {
  const session = await (await fetch("/sessions", { method: "POST" })).json();
  log(`session ${session.session_id} (protocol v${session.protocol_version})`);
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(
    `${proto}://${location.host}${session.control_url}?token=${session.control_token}`
  );
  ws.onopen = () => { log("control socket open"); pushScreenState(); };
  ws.onclose = () => log("control socket closed");
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    log(`< ${ev.data}`);
    if (msg.type === "command") apply(msg);
    else if (msg.type === "agent_status")
      document.getElementById("status").textContent = msg.state;
  };
}

render();
start();
```

- [ ] **Step 3: Mount the static files**

In `src/tv_avatar/app.py`, add these imports at the top:

```python
from pathlib import Path

from fastapi.staticfiles import StaticFiles
```

and insert this immediately before `return app` in `create_app`:

```python
    mock = Path(__file__).resolve().parents[2] / "tools" / "mock_tv_client"
    if mock.is_dir():
        app.mount("/mock", StaticFiles(directory=mock, html=True), name="mock")
```

- [ ] **Step 4: Run the server and exercise it by hand**

```bash
cp .env.example .env   # blank keys are fine; nothing here contacts SLNG or Anam
uv run uvicorn tv_avatar.app:app --reload --port 8000
```

Open `http://localhost:8000/mock/`. Expected: a 2×4 grid, "session … (protocol v1)" and "control socket open" in the log. In a second terminal, drive a command through the bus:

```bash
uv run python -c "
import asyncio, httpx
print(httpx.post('http://localhost:8000/sessions').json())
"
```

The page should keep its socket open and log incoming commands once Task 11 wires the pipeline; for now confirm the socket stays open and `screen_state` is accepted without an `error` reply.

- [ ] **Step 5: Commit**

```bash
git add tools/mock_tv_client/ src/tv_avatar/app.py
git commit -m "feat: add mock TV client speaking the control protocol"
```

---

### Task 11: Avatar and interruption verification — the M1 risk gate

This task exists to answer spec §9 rule 2 and §13's highest-severity unknown: **does `AnamVideoService` stop speaking when the user barges in, or does its startup buffer keep playing?** Everything downstream assumes it does. Find out before building on it.

**Files:**
- Create: `src/tv_avatar/pipeline/runner.py`, `docs/findings/2026-09-19-anam-interruption.md`
- Test: `tests/test_interruption.py`

**Interfaces:**
- Consumes: `build_pipeline`, `CommandBus`, `SessionStore`.
- Produces: `run_session(session, bus, transport) -> None`; a findings document recording measured behaviour.

- [ ] **Step 1: Write the turn-cancellation test (no network needed)**

`tests/test_interruption.py`:

```python
import asyncio

from tv_avatar.control.bus import CommandBus


async def test_barge_in_drops_unsent_commands_but_keeps_sent_ones():
    """Spec §9: queued commands for the interrupted turn are dropped;
    commands already handed to the socket are never rolled back."""
    bus = CommandBus()
    await bus.dispatch("play", {"title_id": "tt_1"}, turn_id="turn_1")
    sent = await bus.next_outbound()          # already delivered
    await bus.dispatch("home", {}, turn_id="turn_1")   # still queued

    dropped = bus.cancel_turn("turn_1")

    assert sent.verb == "play"                 # not rolled back
    assert dropped == 1
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(bus.next_outbound(), timeout=0.05)


async def test_cancelling_one_turn_leaves_the_next_turn_intact():
    bus = CommandBus()
    await bus.dispatch("pause", {}, turn_id="turn_1")
    await bus.dispatch("resume", {}, turn_id="turn_2")
    assert bus.cancel_turn("turn_1") == 1
    assert (await bus.next_outbound()).verb == "resume"
```

Add `import pytest` at the top of the file.

- [ ] **Step 2: Run it and verify it passes**

```bash
uv run pytest tests/test_interruption.py -v
```

Expected: PASS (2 tests) — `CommandBus` already implements this from Task 6.

- [ ] **Step 3: Write `src/tv_avatar/pipeline/runner.py`**

```python
"""Run one session's pipeline to completion."""
from pipecat.pipeline.runner import PipelineRunner

from tv_avatar.control.bus import CommandBus
from tv_avatar.pipeline.builder import build_pipeline
from tv_avatar.session.state import SessionState


async def run_session(
    session: SessionState,
    bus: CommandBus,
    transport,
    *,
    with_avatar: bool = True,
) -> None:
    task = build_pipeline(transport, session, bus, with_avatar=with_avatar)
    await PipelineRunner(handle_sigint=False).run(task)
```

- [ ] **Step 4: Run M0 — the voice loop with no avatar**

Fill real `SLNG_API_KEY` into `.env`, then run with `with_avatar=False` and speak into the mock client's WebRTC connection. Confirm: transcripts appear, the stub's scripted prose is spoken back, and barge-in stops the speech.

- [ ] **Step 5: Run M1 — enable the avatar and measure**

Fill `ANAM_API_KEY` and `ANAM_AVATAR_ID`, set `with_avatar=True`, and record in `docs/findings/2026-09-19-anam-interruption.md`:

1. Does idle video keep flowing between turns? (spec §4 media contract — inferred, not documented)
2. Measured delivered frame rate against the 25 fps target.
3. **On barge-in, does the avatar stop within ~200 ms, or does buffered audio continue?** Record the measured delay.
4. If buffered audio continues: inspect `AnamVideoService` for an interrupt hook (`uv run python -c "import inspect, pipecat_anam; print(inspect.getsource(pipecat_anam.AnamVideoService))" | grep -i interrupt`) and record what exists.

- [ ] **Step 6: Commit the findings**

```bash
git add src/tv_avatar/pipeline/runner.py tests/test_interruption.py docs/findings/
git commit -m "feat: add session runner and record Anam interruption findings"
```

---

## Self-Review

**Spec coverage.** §1 goal → Tasks 9, 11. §2 D1 → Tasks 3, 8. D2 → Tasks 5, 8. D3 → Tasks 2, 6, 7. D4 → Task 7. D5 → Task 9. §3 architecture → Tasks 8, 9. §4 pipeline/services/VAD → Task 9; media contract and frame rate → Task 11 Step 5. §5 turn flow → Tasks 6, 7. §6 lifecycle → Task 8. §7 protocol, versioning, contract distribution, auth → Tasks 3, 4, 8. §8 vocabulary and dispatch semantics → Tasks 2, 6. §9 interruption → Tasks 6, 11. §10 structure and secrets → Tasks 1, 8. §12 testing → every task. §13 prerelease trap → Task 1.

**Deliberately not covered in this plan** (phase 2, per spec §14): the `ScreenContextInjector` frame processor, prompt assembly, real tool handlers, and provider selection. `SessionState.render_for_prompt()` exists and is tested in Task 5 so the injector has a defined input when phase 2 starts.

**Also deferred:** M4 latency/fps instrumentation beyond Task 11's manual measurement, and M5 shoppable products.

**Type consistency check.** `CommandBus.dispatch(verb, args, turn_id)` is called identically in Tasks 6, 7, 10, 11. `parse_command(verb, args)` matches Tasks 2 and 6. `SessionState.update_screen` / `render_for_prompt` / `new_turn` match Tasks 5, 8, 9. `bus_for(session_id)` matches Tasks 8, 9. `build_pipeline(transport, session, bus, *, with_avatar)` matches Tasks 9 and 11.

**Known soft spots, flagged rather than hidden.** Task 1 Step 6 exists because Pipecat's `LLMService` import path is version-sensitive and Task 7 subclasses it; Task 9 Step 4 exists because the SLNG and Anam constructor signatures come from documentation that has already proven stale on the version floor. Both steps give the exact introspection command to get the truth from the installed package rather than guessing.
