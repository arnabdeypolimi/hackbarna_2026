import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from tv_avatar.agent.commands import Verb
from tv_avatar.agent.envelope import (
    REGISTRY,
    RecommendTitles,
    RejectTitle,
    TurnPlan,
    describe_capabilities,
    parse_action,
    turn_plan_schema,
)

SNAPSHOT = Path(__file__).parent / "fixtures" / "turn_plan_schema.json"
FINAL_SNAPSHOT = Path(__file__).parent / "fixtures" / "turn_plan_final_schema.json"


def test_action_union_covers_tv_and_internal_verbs():
    plan = TurnPlan.model_validate({
        "intent": "recommend", "say": "one sec",
        "actions": [{"verb": "recommend_titles", "query": "heist"},
                    {"verb": "focus", "title_id": "27205"}]})
    assert [a.verb for a in plan.actions] == ["recommend_titles", "focus"]


def test_registry_covers_every_union_member():
    """One entry per action model, keyed by the model's own `verb` literal."""
    assert {v.value for v in Verb} <= set(REGISTRY)
    for verb, spec in REGISTRY.items():
        assert spec.model.model_fields["verb"].default == verb
    items = turn_plan_schema()["schema"]["properties"]["actions"]["items"]
    assert len(items["anyOf"]) == len(REGISTRY)


def test_registry_classification():
    tv = REGISTRY["search_catalog"]
    assert tv.kind == "tv" and tv.awaits_result and not tv.returns_observation
    assert REGISTRY["play"].kind == "tv" and not REGISTRY["play"].awaits_result
    rec = REGISTRY["recommend_titles"]
    assert rec.kind == "internal" and rec.awaits_result and rec.returns_observation
    rej = REGISTRY["reject_title"]
    assert rej.kind == "internal" and not rej.awaits_result and not rej.returns_observation
    # A result can only be an observation if the turn waited for it.
    assert all(spec.awaits_result for spec in REGISTRY.values() if spec.returns_observation)
    assert {v for v, s in REGISTRY.items() if s.kind == "tv"} == {v.value for v in Verb}


def test_final_schema_has_no_observation_tools():
    """The cycle cap is the schema: the last cycle cannot ask for another one."""
    wrapped = turn_plan_schema(final=True)
    assert wrapped["name"] == "turn_plan_final" and wrapped["strict"] is True
    items = wrapped["schema"]["properties"]["actions"]["items"]
    assert len(items["anyOf"]) == len(REGISTRY) - 1
    assert "RecommendTitles" not in wrapped["schema"]["$defs"]
    assert list(wrapped["schema"]["properties"]) == ["intent", "say", "actions"]
    with pytest.raises(ValidationError):
        parse_action({"verb": "recommend_titles", "query": "heist"}, final=True)
    assert isinstance(parse_action({"verb": "reject_title", "title_id": "1"}, final=True), RejectTitle)
    assert parse_action({"verb": "focus", "title_id": "27205"}, final=True).verb == Verb.FOCUS


def test_turn_plan_final_schema_unchanged():
    """Also sent to the constrained decoder — same guard as the full schema."""
    expected = json.loads(FINAL_SNAPSHOT.read_text())
    assert json.loads(json.dumps(turn_plan_schema(final=True), sort_keys=True)) == expected


def test_parse_action_returns_typed_model():
    assert isinstance(parse_action({"verb": "recommend_titles", "query": "heist"}), RecommendTitles)
    assert isinstance(parse_action({"verb": "reject_title", "title_id": "1"}), RejectTitle)
    assert parse_action({"verb": "focus", "title_id": "27205"}).verb == Verb.FOCUS
    with pytest.raises(ValidationError):
        parse_action({"verb": "reboot"})
    with pytest.raises(ValidationError):
        parse_action({"verb": "seek"})  # neither target


def test_unknown_verb_is_unrepresentable():
    with pytest.raises(ValidationError):
        TurnPlan.model_validate({"intent": "control", "say": "", "actions": [{"verb": "reboot"}]})


def test_schema_is_strict_and_ordered():
    wrapped = turn_plan_schema()
    assert wrapped["strict"] is True and wrapped["name"] == "turn_plan"
    schema = wrapped["schema"]
    assert list(schema["properties"]) == ["intent", "say", "actions"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["intent", "say", "actions"]
    text = json.dumps(schema)
    assert '"oneOf"' not in text and '"discriminator"' not in text and '"default"' not in text
    for definition in schema["$defs"].values():
        if definition.get("type") == "object":
            assert definition["additionalProperties"] is False
            assert set(definition["required"]) == set(definition["properties"])


def test_turn_plan_schema_unchanged():
    """The constrained-decoding contract sent to Nebius. Changing it is a
    re-bench of every model in the findings table, not a refactor — regenerate
    the fixture deliberately (see tests/fixtures/) if that is what you mean."""
    expected = json.loads(SNAPSHOT.read_text())
    assert json.loads(json.dumps(turn_plan_schema(), sort_keys=True)) == expected


def test_capabilities_manifest_from_registry():
    text = describe_capabilities()
    lines = [line for line in text.splitlines() if line.startswith("- ")]
    assert len(lines) == len(REGISTRY)
    for verb, spec in REGISTRY.items():
        line = next(line for line in lines if line.startswith(f"- {verb}"))
        assert spec.doc in line
        assert ("[awaits result]" in line) == spec.awaits_result
        assert ("INTERNAL" in line) == (spec.kind == "internal")


def test_recommend_titles_genres_are_constrained_to_tmdb_enum():
    ok = RecommendTitles(genres=["Crime", "Thriller"], exclude_genres=["Horror"])
    assert ok.exclude_genres == ["Horror"]
    with pytest.raises(ValidationError):
        RecommendTitles(genres=["crime thriller"])
    schema = json.dumps(turn_plan_schema()["schema"])
    assert '"Science Fiction"' in schema and '"exclude_genres"' in schema
