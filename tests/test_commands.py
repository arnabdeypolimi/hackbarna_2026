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
