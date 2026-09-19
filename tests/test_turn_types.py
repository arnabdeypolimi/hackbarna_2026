"""The typed turn state that replaced the `marks` dict and (verb, dict) tuples."""
from tv_avatar.agent.turn import CycleOutcome, ToolResult, TurnMetrics


def test_metrics_log_fields_omit_unset_and_false_fallback():
    m = TurnMetrics()
    assert m.as_log_fields() == {"cycles": 0, "n_actions": 0, "intent": None}
    m.cycles, m.intent, m.recall_ms = 2, "recommend", 3
    m.fallback = True
    assert m.as_log_fields() == {"cycles": 2, "n_actions": 0, "intent": "recommend", "recall_ms": 3, "fallback": True}


def test_mark_once_keeps_the_first_value():
    m = TurnMetrics()
    m.mark_once("ttft_ms", 300)
    m.mark_once("ttft_ms", 900)
    assert m.ttft_ms == 300
    m.mark_once("first_action_ms", 500)
    assert m.first_action_ms == 500


def test_tool_result_knows_whether_it_earns_a_cycle():
    assert ToolResult("recommend_titles", {"titles": []}).earns_cycle
    assert ToolResult("recall_memory", {"status": "error"}).earns_cycle   # a failure still needs speaking
    assert not ToolResult("search_catalog", {"status": "ok"}).earns_cycle
    assert not ToolResult("reject_title", {}).earns_cycle
    assert not ToolResult("not_a_verb", {}).earns_cycle


def test_cycle_outcome_feedback_is_keyed_by_verb():
    outcome = CycleOutcome(raw="{}", results=(ToolResult("recommend_titles", {"titles": [1]}),
                                              ToolResult("recall_memory", {"memory": "x"})))
    assert outcome.needs_another_cycle
    assert outcome.feedback() == '{"recommend_titles": {"titles": [1]}, "recall_memory": {"memory": "x"}}'
    assert not CycleOutcome(raw="{}", results=()).needs_another_cycle
