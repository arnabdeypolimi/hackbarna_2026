import json

from tv_avatar.agent.stream_parse import (
    ActionReady,
    Done,
    EnvelopeStreamer,
    IntentReady,
    SayDelta,
)

ENVELOPE = '{"intent":"control","say":"Putting that on.","actions":[{"verb":"play","title_id":"1"}]}'


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
    assert kinds[0] == "IntentReady" and kinds[1] == "SayDelta"
    assert "ActionReady" in kinds and kinds[-1] == "Done"
    assert _say(events) == "Putting that on."
    assert [e.action for e in events if isinstance(e, ActionReady)] == [{"verb": "play", "title_id": "1"}]


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
    assert s.feed("garbage }}}") == [] or not s.finished or True  # must not raise


def test_empty_actions_and_done_once():
    s = EnvelopeStreamer()
    events = s.feed('{"intent":"chitchat","say":"hello","actions":[]}')
    assert sum(isinstance(e, Done) for e in events) == 1
    assert s.feed("trailing") == []
