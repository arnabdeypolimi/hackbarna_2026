import json

import pytest
from pydantic import ValidationError

from tv_avatar.agent.commands import Verb
from tv_avatar.agent.envelope import (
    ALL_MODELS,
    AWAITED_VERBS,
    TurnPlan,
    describe_capabilities,
    turn_plan_schema,
)


def test_action_union_covers_tv_and_internal_verbs():
    plan = TurnPlan.model_validate({
        "intent": "recommend", "say": "one sec",
        "actions": [{"verb": "recommend_titles", "query": "heist"},
                    {"verb": "focus", "title_id": "27205"}]})
    assert [a.verb for a in plan.actions] == ["recommend_titles", "focus"]


def test_every_tv_verb_is_in_the_union():
    assert {v.value for v in Verb} <= set(ALL_MODELS)
    assert "search_catalog" in AWAITED_VERBS and "recommend_titles" in AWAITED_VERBS
    assert "play" not in AWAITED_VERBS


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
    items = schema["properties"]["actions"]["items"]
    assert "anyOf" in items and len(items["anyOf"]) == len(ALL_MODELS)
    for definition in schema["$defs"].values():
        if definition.get("type") == "object":
            assert definition["additionalProperties"] is False
            assert set(definition["required"]) == set(definition["properties"])


def test_capabilities_manifest_lists_every_verb():
    text = describe_capabilities()
    for verb in ALL_MODELS:
        assert f"- {verb}" in text
    assert "INTERNAL" in text and "[awaits result]" in text
