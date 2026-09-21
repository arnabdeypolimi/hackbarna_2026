import json

from tv_avatar.agent.stream_parse import (
    ActionReady,
    Done,
    EnvelopeStreamer,
    IntentReady,
    RequestReady,
    SayDelta,
    SayDone,
)

ENVELOPE = '{"intent":"control","request":{"operation":"play","title":null,"title_id":"1"},"say":"Putting that on.","actions":[{"verb":"play","title_id":"1"}]}'


def _feed_chars(text: str) -> list:
    s = EnvelopeStreamer()
    events = []
    for ch in text:
        events += s.feed(ch)
    return events


def _say(events) -> str:
    return "".join(e.text for e in events if isinstance(e, SayDelta))


def test_streamer_emits_say_deltas_then_actions():
    events = _feed_chars(ENVELOPE)
    kinds = [type(e).__name__ for e in events]
    assert kinds[:3] == ["IntentReady", "RequestReady", "SayDelta"]
    assert "ActionReady" in kinds and kinds[-1] == "Done"
    assert _say(events) == "Putting that on."
    assert [e.action for e in events if isinstance(e, ActionReady)] == [{"verb": "play", "title_id": "1"}]


def test_request_object_is_emitted_whole_before_say_and_never_as_an_action():
    """The cascade's second step: `request` closes before `say` opens, so the
    loop knows the operation before the first action streams. Chunk boundaries
    inside it and a nested "request" key elsewhere must not confuse it."""
    events = _feed_chars(ENVELOPE)
    kinds = [type(e).__name__ for e in events]
    assert kinds.index("RequestReady") < kinds.index("SayDelta") < kinds.index("ActionReady")
    (request,) = [e.request for e in events if isinstance(e, RequestReady)]
    assert request == {"operation": "play", "title": None, "title_id": "1"}
    s = EnvelopeStreamer()
    split = s.feed(ENVELOPE[:30]) + s.feed(ENVELOPE[30:52]) + s.feed(ENVELOPE[52:])
    assert [e.request for e in split if isinstance(e, RequestReady)] == [request]
    nested = ('{"intent":"search","request":{"operation":"lookup","title":"a {b} \\"request\\"","title_id":null},'
              '"say":"ok","actions":[{"verb":"search_catalog","query":"request"}]}')
    events = _feed_chars(nested)
    assert [e.request["title"] for e in events if isinstance(e, RequestReady)] == ['a {b} "request"']
    assert [e.action for e in events if isinstance(e, ActionReady)] == [{"verb": "search_catalog", "query": "request"}]


def test_say_done_follows_the_last_delta_and_precedes_actions():
    for events in (_feed_chars(ENVELOPE), EnvelopeStreamer().feed(ENVELOPE)):
        kinds = [type(e).__name__ for e in events]
        assert kinds.count("SayDone") == 1
        i = kinds.index("SayDone")
        assert "SayDelta" not in kinds[i:] and "ActionReady" not in kinds[:i]
    # Whole envelope in one chunk: the delta gathered across the chunk still lands before SayDone.
    whole = EnvelopeStreamer().feed(ENVELOPE)
    assert [type(e) for e in whole[:4]] == [IntentReady, RequestReady, SayDelta, SayDone]
    assert not any(isinstance(e, SayDone) for e in _feed_chars('{"intent":"chitchat","actions":[]}'))


def test_chunk_boundaries_do_not_matter():
    whole = EnvelopeStreamer().feed(ENVELOPE)
    assert _say(whole) == "Putting that on."
    assert isinstance(whole[-1], Done)
    s = EnvelopeStreamer()
    events = s.feed(ENVELOPE[:17]) + s.feed(ENVELOPE[17:40]) + s.feed(ENVELOPE[40:])
    assert _say(events) == "Putting that on."
    assert sum(isinstance(e, ActionReady) for e in events) == 1


def test_actions_before_say_still_works():
    text = '{"actions":[{"verb":"pause"},{"verb":"home"}],"say":"Paused, going home.","intent":"control"}'
    events = _feed_chars(text)
    verbs = [e.action["verb"] for e in events if isinstance(e, ActionReady)]
    assert verbs == ["pause", "home"]
    assert _say(events) == "Paused, going home."
    assert isinstance(events[-1], Done)
    assert any(isinstance(e, IntentReady) and e.intent == "control" for e in events)


def test_escapes_and_unicode_in_say():
    text = json.dumps({"intent": "chitchat", "say": 'He said "hi"\nnaïve ☃ \\', "actions": []})
    assert _say(_feed_chars(text)) == 'He said "hi"\nnaïve ☃ \\'


def test_nested_action_objects_and_braces_in_strings():
    text = ('{"intent":"search","say":"ok","actions":[{"verb":"search_catalog","query":"a {weird] \\"q\\"","limit":3},'
            '{"verb":"seek","to_seconds":12.5,"delta_seconds":null}]}')
    actions = [e.action for e in _feed_chars(text) if isinstance(e, ActionReady)]
    assert actions[0]["query"] == 'a {weird] "q"'
    assert actions[1] == {"verb": "seek", "to_seconds": 12.5, "delta_seconds": None}


def test_whitespace_pretty_printed_envelope():
    text = json.dumps({"intent": "control", "say": "hey", "actions": [{"verb": "pause"}]}, indent=2)
    events = _feed_chars(text)
    assert _say(events) == "hey"
    assert [e.action for e in events if isinstance(e, ActionReady)] == [{"verb": "pause"}]


def test_malformed_input_never_raises_and_emits_nothing_after():
    s = EnvelopeStreamer()
    events = s.feed('{"say":"hi","actions":[{"verb":"play" "oops"}]}')
    assert not any(isinstance(e, ActionReady) for e in events)
    assert _say(events) == "hi"
    s.feed("garbage }}}")  # must not raise


def test_empty_actions_and_done_once():
    s = EnvelopeStreamer()
    events = s.feed('{"intent":"chitchat","say":"hello","actions":[]}')
    assert sum(isinstance(e, Done) for e in events) == 1
    assert s.feed("trailing") == []
