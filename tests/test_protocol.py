import json

import pytest

from tv_avatar.control.protocol import (
    PROTOCOL_VERSION,
    CommandMsg,
    ProtocolError,
    ScreenStateMsg,
    parse_client_message,
)


def _screen_state_payload(**over):
    payload = {
        "v": PROTOCOL_VERSION,
        "type": "screen_state",
        "state": {
            "view": "grid",
            "rail_id": "rail_trending",
            "focus_index": 1,
            "tiles": [
                {"title_id": "tt_1", "name": "Heat", "position": 0},
                {"title_id": "tt_2", "name": "Sicario", "position": 1},
            ],
            "playback": {"state": "stopped", "title_id": None, "position_s": 0.0},
        },
    }
    payload.update(over)
    return json.dumps(payload)


def test_parses_screen_state():
    msg = parse_client_message(_screen_state_payload())
    assert isinstance(msg, ScreenStateMsg)
    assert msg.state.focus_index == 1
    assert msg.state.tiles[1].name == "Sicario"


def test_rejects_unknown_protocol_version():
    with pytest.raises(ProtocolError) as exc:
        parse_client_message(_screen_state_payload(v=99))
    assert exc.value.code == "unsupported_version"


def test_rejects_malformed_json():
    with pytest.raises(ProtocolError) as exc:
        parse_client_message("{not json")
    assert exc.value.code == "malformed"


def test_rejects_unknown_message_type():
    with pytest.raises(ProtocolError) as exc:
        parse_client_message(json.dumps({"v": 1, "type": "launch_missiles"}))
    assert exc.value.code == "invalid"


def test_command_message_serialises_with_version():
    msg = CommandMsg(
        id="cmd_7",
        turn_id="turn_3",
        verb="play",
        args={"title_id": "tt_88"},
        ts=1758278400.123,
    )
    body = json.loads(msg.model_dump_json())
    assert body["v"] == PROTOCOL_VERSION
    assert body["type"] == "command"
    assert body["args"]["title_id"] == "tt_88"
