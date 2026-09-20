"""The typed turn state that replaced the `marks` dict and (verb, dict) tuples."""
from tv_avatar.agent.turn import CycleOutcome, ToolResult, TurnMetrics, TurnTrace


def _reco(*names: tuple[str, str]) -> ToolResult:
    return ToolResult("recommend_titles", {"titles": [{"title_id": i, "name": n} for i, n in names]})


def test_offered_ids_are_the_candidates_named_or_pointed_at():
    trace = TurnTrace()
    trace.add_results((_reco(("949", "Heat"), ("27205", "Inception"), ("680", "Pulp Fiction"),
                             ("8", "Drive"), ("9", "Extra")),))
    trace.said += ["Let me look.", "Try Heat or Inception."]
    trace.add_action("focus", {"title_id": "949"}, after_results=True)
    assert trace.offered_ids() == ["949", "27205"]


def test_offered_ids_put_the_pointed_at_titles_before_the_merely_named_ones():
    """The greeting reopens with the first offered id, so the focused title must
    lead even when it came later in the tool results."""
    trace = TurnTrace()
    trace.add_results((_reco(("1", "Saw X"), ("2", "Talk to Me"), ("3", "The Nun II")),))
    trace.said += ["The Nun II, Talk to Me, or Saw X."]
    trace.add_action("focus", {"title_id": "3"}, after_results=True)
    assert trace.offered_ids() == ["3", "1", "2"]
    # Two pointed-at titles keep candidate order between themselves.
    trace.add_action("open_details", {"title_id": "2"}, after_results=True)
    assert trace.offered_ids() == ["2", "3", "1"]


def test_offered_ids_match_names_case_insensitively_and_from_fallback_text():
    trace = TurnTrace()
    trace.add_results((_reco(("1", "The Nun II"), ("2", "Saw X")),))
    assert trace.offered_ids() == []
    trace.fallback_said = "How about THE NUN II from 2023?"
    assert trace.offered_ids() == ["1"]
    assert trace.spoken() == ""  # the template is not the agent's reply


def test_begin_cycle_separates_cycles_in_the_transcript():
    trace = TurnTrace()
    trace.begin_cycle()
    trace.said.append("Let me look.")
    trace.begin_cycle()
    trace.said.append("Try Heat.")
    assert trace.spoken() == "Let me look. Try Heat."


def test_actions_before_results_do_not_count_as_offers():
    """A cycle-1 `focus` targets the screen, not a recommendation."""
    trace = TurnTrace()
    trace.add_results((_reco(("949", "Heat"),),))
    trace.add_action("focus", {"title_id": "949"}, after_results=False)
    trace.add_action("pause", {}, after_results=True)
    trace.add_action("focus", {"title_id": "not-a-candidate"}, after_results=True)
    assert trace.offered_ids() == []


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


def test_intent_is_the_first_cycles_routing_decision_not_the_answer_cycles():
    m = TurnMetrics()
    m.mark_once("intent", "recommend")
    m.mark_once("intent", "answer")
    assert m.intent == "recommend"


def test_tool_result_knows_whether_it_is_an_observation():
    assert ToolResult("recommend_titles", {"titles": []}).is_observation
    assert ToolResult("recommend_titles", {"status": "error"}).is_observation   # a failure still needs speaking
    assert not ToolResult("search_catalog", {"status": "ok"}).is_observation
    assert not ToolResult("search_catalog", {"titles": []}).is_observation      # the TV answered; it shows them
    assert not ToolResult("reject_title", {}).is_observation
    assert not ToolResult("not_a_verb", {}).is_observation


def test_failed_awaited_tv_verb_is_an_observation_so_the_viewer_hears_it():
    assert ToolResult("search_catalog", {"status": "unavailable", "reason": "timeout"}).is_observation
    assert ToolResult("search_catalog", {"status": "error", "reason": "boom"}).is_observation
    assert ToolResult("search_catalog", {"status": "invalid", "reason": "bad args"}).is_observation
    # A barge-in releases the wait with "cancelled": the turn is dying, do not speak again.
    assert not ToolResult("search_catalog", {"status": "cancelled", "reason": "interrupted"}).is_observation
    # Fire-and-forget verbs never enter the results; even a failure status is not an observation.
    assert not ToolResult("play", {"status": "invalid"}).is_observation


def test_cycle_outcome_is_done_without_observations_and_feeds_them_back_by_verb():
    outcome = CycleOutcome(raw="{}", observations=(ToolResult("recommend_titles", {"titles": [1]}),
                                                   ToolResult("search_catalog", {"status": "unavailable"})))
    assert not outcome.done
    assert outcome.feedback() == '{"recommend_titles": {"titles": [1]}, "search_catalog": {"status": "unavailable"}}'
    assert CycleOutcome(raw="{}", observations=()).done


def test_turn_metrics_span_attributes_drop_none_and_prefix():
    """Filter keys under Langfuse observation metadata, timings as tv.turn.* (D16/D20)."""
    m = TurnMetrics(cycles=1, n_actions=1, intent="control", ttft_ms=420)
    attrs = m.as_span_attributes()
    assert attrs["langfuse.observation.metadata.cycles"] == 1 and attrs["tv.turn.ttft_ms"] == 420
    assert attrs["langfuse.observation.metadata.intent"] == "control" and attrs["tv.turn.n_actions"] == 1
    assert "tv.turn.recall_ms" not in attrs and "langfuse.observation.metadata.fallback" not in attrs
    assert None not in TurnMetrics().as_span_attributes().values()
    assert TurnMetrics(fallback=True).as_span_attributes()["langfuse.observation.metadata.fallback"] is True
