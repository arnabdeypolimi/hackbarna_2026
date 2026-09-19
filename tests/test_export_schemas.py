import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.export_schemas import build_schema_bundle, render_typescript
from tv_avatar.agent.commands import Verb


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


def test_typescript_types_every_client_message():
    ts = render_typescript(build_schema_bundle())
    assert "export type ClientMessage = ScreenStateMsg | AckMsg | ResultMsg | UserEventMsg;" in ts
    for name in ("ScreenStateMsg", "AckMsg", "ResultMsg", "UserEventMsg", "ScreenState", "Tile", "Playback"):
        assert f"export interface {name} {{" in ts


def test_typescript_types_command_args_per_verb_not_as_unknown_record():
    ts = render_typescript(build_schema_bundle())
    assert "export interface PlayArgs {\n  title_id: string;\n  resume_from?: number | null;\n}" in ts
    assert "export interface SeekArgs {" in ts and "to_seconds?: number | null;" in ts
    assert 'direction: "up" | "down" | "left" | "right";' in ts
    for verb in Verb:
        assert f'  "{verb.value}": ' in ts  # CommandArgsByVerb entry
    assert "args: CommandArgsByVerb[V]" in ts
    assert "args: Record<string, unknown>" not in ts


def test_typescript_wire_constants_are_literal_types():
    ts = render_typescript(build_schema_bundle())
    assert "  v: 1;" in ts
    assert '  type: "screen_state";' in ts
    assert '  type: "command";' in ts


def test_typescript_server_messages_have_no_optional_fields():
    # The server serialises every field; the TV app may rely on all of them.
    ts = render_typescript(build_schema_bundle())
    assert "  ts: number;" in ts
    assert "export interface PauseArgs {}" in ts
