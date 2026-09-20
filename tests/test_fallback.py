"""The templated answer spoken when a follow-up cycle blows its budget."""
from tv_avatar.agent.fallback import render_fallback
from tv_avatar.agent.turn import ToolResult


def test_failed_recommendation_is_reported_as_a_failure_not_as_no_match():
    """A timed-out recs engine has no titles, but it did not "find nothing"."""
    text, actions = render_fallback((ToolResult("recommend_titles", {"status": "unavailable", "reason": "timeout"}),))
    assert "couldn't find" not in text and "try again" in text and actions == []
    text, _ = render_fallback((ToolResult("recommend_titles", {"status": "error", "reason": "TypeError"}),))
    assert "didn't go through" in text


def test_failed_tv_search_names_the_tv():
    text, actions = render_fallback((ToolResult("search_catalog", {"status": "unavailable"}),))
    assert "the TV didn't respond" in text and actions == []
