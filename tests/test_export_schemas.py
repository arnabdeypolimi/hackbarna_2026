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
