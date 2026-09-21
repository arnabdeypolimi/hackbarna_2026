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


DISCOVER = {"operation": "discover", "title": None, "title_id": None}


def test_action_union_covers_tv_and_internal_verbs():
    plan = TurnPlan.model_validate({
        "intent": "recommend", "request": DISCOVER, "say": "one sec",
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
    assert tv.kind == "tv" and tv.awaits_result and tv.returns_observation
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
    assert len(items["anyOf"]) == len(REGISTRY) - 2
    assert "RecommendTitles" not in wrapped["schema"]["$defs"]
    assert "SearchCatalog" not in wrapped["schema"]["$defs"]
    with pytest.raises(ValidationError):
        parse_action({"verb": "search_catalog", "query": "space"}, final=True)
    assert parse_action({"verb": "show_titles", "title_ids": ["1"]}, final=True).verb == Verb.SHOW_TITLES
    assert list(wrapped["schema"]["properties"]) == ["intent", "request", "say", "actions"]
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
        TurnPlan.model_validate({"intent": "control", "request": DISCOVER, "say": "",
                                 "actions": [{"verb": "reboot"}]})


def test_schema_is_strict_and_ordered():
    wrapped = turn_plan_schema()
    assert wrapped["strict"] is True and wrapped["name"] == "turn_plan"
    schema = wrapped["schema"]
    assert list(schema["properties"]) == ["intent", "request", "say", "actions"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["intent", "request", "say", "actions"]
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


def test_plan_decodes_the_request_before_choosing_actions():
    """The operation the viewer asked for is decoded, not inferred from `say`."""
    from tv_avatar.agent.envelope import FinalTurnPlan, Request

    schema = turn_plan_schema()["schema"]
    assert list(schema["properties"]) == ["intent", "request", "say", "actions"]
    request = schema["$defs"]["Request"]
    assert request["properties"]["operation"]["enum"] == [
        "play", "open", "lookup", "discover", "shop", "control", "answer"]
    assert set(request["required"]) == {"operation", "title", "title_id"}
    assert list(turn_plan_schema(final=True)["schema"]["properties"]) == [
        "intent", "request", "say", "actions"]

    plan = TurnPlan.model_validate({
        "intent": "control", "request": {"operation": "play", "title": "Barbie", "title_id": "346698"},
        "say": "On it.", "actions": [{"verb": "play", "title_id": "346698"}]})
    assert (plan.request.operation, plan.request.title_id) == ("play", "346698")
    assert Request(operation="answer", title=None, title_id=None).title is None
    with pytest.raises(ValidationError):
        Request(operation="rewind", title=None, title_id=None)
    assert FinalTurnPlan.model_validate({
        "intent": "search", "request": {"operation": "lookup", "title": "Moon", "title_id": None},
        "say": "I found Moon.",
        "actions": [{"verb": "show_titles", "title_ids": ["17431"]}]}).request.operation == "lookup"


def test_strictify_keeps_fields_whose_names_are_schema_keywords():
    """`title` is a JSON Schema annotation *and* a field name here; stripping
    annotations must not shrink the decoding contract."""
    from pydantic import BaseModel

    from tv_avatar.sgr import strictify

    class Inner(BaseModel):
        title: str
        default: int = 3

    schema = strictify(Inner.model_json_schema())
    assert set(schema["properties"]) == {"title", "default"}
    assert set(schema["required"]) == {"title", "default"}
    assert "title" not in schema  # the model's own annotation is still dropped


def test_actions_must_serve_the_decoded_request():
    """The cascade's validation step: verbs with a consequence need the request
    that licenses them, title-directed verbs need the decoded id, and the
    harmless rest (search, rails, focus) is free whatever the request."""
    from tv_avatar.agent.envelope import plan_violations

    def plan(operation, title_id, actions):
        return TurnPlan.model_validate({
            "intent": "control", "say": "ok",
            "request": {"operation": operation, "title": "Barbie", "title_id": title_id},
            "actions": actions})

    play = {"verb": "play", "title_id": "346698"}
    assert plan_violations(plan("play", "346698", [play])) == []
    assert plan_violations(plan("open", "346698", [play])) == ["play does not serve an open request"]
    assert plan_violations(plan("lookup", "17431", [play])) == ["play does not serve a lookup request"]
    assert plan_violations(plan("play", "346698", [{"verb": "show_products", "title_id": "346698"}])) == [
        "show_products does not serve a play request"]
    assert plan_violations(plan("play", "346698", [
        {"verb": "recommend_titles", "query": "colorful"}])) == [
        "recommend_titles does not serve a play request"]
    # No id decoded means "search first", never "guess"; a different id is a substitution.
    assert plan_violations(plan("play", None, [{"verb": "search_catalog", "query": "Barbie"}])) == []
    assert plan_violations(plan("play", None, [play])) == ["play before the title's id is known"]
    assert plan_violations(plan("play", "27205", [play])) == ["play targets 346698, not the requested 27205"]
    # A single lookup hit may open its details; a focus ahead of open_details is cosmetic.
    assert plan_violations(plan("lookup", "17431", [{"verb": "open_details", "title_id": "17431"}])) == []
    assert plan_violations(plan("open", "346698", [
        {"verb": "focus", "title_id": "346698"}, {"verb": "open_details", "title_id": "346698"}])) == []
    assert plan_violations(plan("play", None, [{"verb": "show_titles", "title_ids": ["10001", "10002"]}])) == []
    # Unrelated requests keep their existing freedom.
    assert plan_violations(plan("discover", None, [{"verb": "recommend_titles", "query": "heist"}])) == []
    assert plan_violations(plan("control", None, [{"verb": "pause"}])) == []
    assert plan_violations(plan("answer", None, [])) == []
    assert plan_violations(plan("answer", None, [{"verb": "reject_title", "title_id": "1"}])) == [
        "reject_title does not serve an answer request"]


def test_recommend_titles_genres_are_constrained_to_tmdb_enum():
    ok = RecommendTitles(genres=["Crime", "Thriller"], exclude_genres=["Horror"])
    assert ok.exclude_genres == ["Horror"]
    with pytest.raises(ValidationError):
        RecommendTitles(genres=["crime thriller"])
    schema = json.dumps(turn_plan_schema()["schema"])
    assert '"Science Fiction"' in schema and '"exclude_genres"' in schema


def test_a_follow_up_schema_pins_the_operation_the_viewer_asked_for():
    """Results arriving in cycle 2 may resolve the title, never change the operation."""
    pinned = turn_plan_schema(operation="discover")
    assert pinned["name"] == "turn_plan_discover"
    assert pinned["schema"]["$defs"]["Request"]["properties"]["operation"]["const"] == "discover"
    assert "const" not in turn_plan_schema()["schema"]["$defs"]["Request"]["properties"]["operation"]
    final = turn_plan_schema(final=True, operation="lookup")
    assert final["name"] == "turn_plan_final_lookup"
    assert final["schema"]["$defs"]["Request"]["properties"]["operation"]["const"] == "lookup"
