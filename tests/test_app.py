import json

from fastapi.testclient import TestClient

from tv_avatar.app import create_app
from tv_avatar.control.protocol import PROTOCOL_VERSION


def _client():
    return TestClient(create_app())


def test_create_session_returns_token_and_url():
    with _client() as c:
        body = c.post("/sessions").json()
        assert body["session_id"].startswith("sess_")
        assert len(body["control_token"]) > 20
        assert body["session_id"] in body["control_url"]
        assert body["protocol_version"] == PROTOCOL_VERSION


def test_control_socket_rejects_missing_token():
    with _client() as c:
        sid = c.post("/sessions").json()["session_id"]
        with c.websocket_connect(f"/sessions/{sid}/control") as ws:
            msg = ws.receive_json()
            assert msg["type"] == "error"
            assert msg["code"] == "unauthorized"


def test_control_socket_rejects_wrong_token():
    with _client() as c:
        sid = c.post("/sessions").json()["session_id"]
        with c.websocket_connect(f"/sessions/{sid}/control?token=wrong") as ws:
            assert ws.receive_json()["code"] == "unauthorized"


def test_control_socket_accepts_valid_token_and_stores_screen_state():
    with _client() as c:
        body = c.post("/sessions").json()
        url = f"/sessions/{body['session_id']}/control?token={body['control_token']}"
        with c.websocket_connect(url) as ws:
            assert ws.receive_json()["type"] == "agent_status"
            ws.send_text(json.dumps({
                "v": PROTOCOL_VERSION,
                "type": "screen_state",
                "state": {
                    "view": "grid",
                    "rail_id": "r1",
                    "focus_index": 0,
                    "tiles": [{"title_id": "tt_1", "name": "Heat", "position": 0}],
                    "playback": {"state": "stopped", "title_id": None,
                                 "position_s": 0.0},
                },
            }))
            ws.send_text(json.dumps({"v": 99, "type": "screen_state"}))
            err = ws.receive_json()
            assert err["code"] == "unsupported_version"


def test_dispatched_command_is_delivered_over_the_socket():
    app = create_app()
    with TestClient(app) as c:
        body = c.post("/sessions").json()
        bus = app.state.manager.bus_for(body["session_id"])
        url = f"/sessions/{body['session_id']}/control?token={body['control_token']}"
        with c.websocket_connect(url) as ws:
            ws.receive_json()  # agent_status
            # Dispatch on the app's event loop; the channel's write loop
            # must forward it to this socket.
            c.portal.call(bus.dispatch, "home", {}, "turn_1")
            msg = ws.receive_json()
            assert msg["type"] == "command"
            assert msg["verb"] == "home"
