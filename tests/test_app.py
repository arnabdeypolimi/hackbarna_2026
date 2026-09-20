import json

import pytest
from fastapi.testclient import TestClient

from tv_avatar.app import create_app
from tv_avatar.control.protocol import PROTOCOL_VERSION


def _client():
    return TestClient(create_app())


def test_create_session_returns_token_and_urls():
    with _client() as c:
        body = c.post("/sessions").json()
        assert body["session_id"].startswith("sess_")
        assert len(body["control_token"]) > 20
        assert body["session_id"] in body["control_url"]
        assert body["offer_url"] == f"/sessions/{body['session_id']}/offer"
        assert body["protocol_version"] == PROTOCOL_VERSION


def test_create_session_defaults_to_catalog_avatar_and_language():
    from tv_avatar.catalog import get_catalog
    cat = get_catalog()
    app = create_app()
    with TestClient(app) as c:
        body = c.post("/sessions").json()
        assert body["avatar"] == cat.default_avatar
        assert body["language"] == cat.default_language
        persona = app.state.store.get(body["session_id"]).persona
        assert persona.avatar.id == cat.default_avatar


def test_create_session_pins_requested_avatar_and_language():
    from tv_avatar.catalog import get_catalog
    avatar = get_catalog().avatars[0].id
    app = create_app()
    with TestClient(app) as c:
        body = c.post("/sessions", json={"avatar": avatar, "language": "ca"}).json()
        assert (body["avatar"], body["language"]) == (avatar, "ca")
        persona = app.state.store.get(body["session_id"]).persona
        assert persona.language.code == "ca"
        assert persona.avatar.voice == get_catalog().avatar(avatar).voice


def test_create_session_rejects_unknown_avatar_or_language():
    with _client() as c:
        res = c.post("/sessions", json={"avatar": "nobody"})
        assert res.status_code == 422
        assert "nobody" in res.json()["detail"]
        # Not in the LanguageCode literal: pydantic rejects before we look it up.
        assert c.post("/sessions", json={"language": "de"}).status_code == 422


def test_app_refuses_to_start_on_a_broken_catalog(monkeypatch, tmp_path):
    """A bad avatars.yaml must fail at boot, like a bad .env, not on first request."""
    from pydantic import ValidationError
    from tv_avatar.catalog import get_catalog
    broken = tmp_path / "avatars.yaml"
    broken.write_text("default_avatar: ghost\nlanguages: []\navatars: []\n", encoding="utf-8")
    monkeypatch.setenv("AVATARS_FILE", str(broken))
    get_catalog.cache_clear()
    try:
        with pytest.raises(ValidationError):
            create_app()
    finally:
        get_catalog.cache_clear()


def test_create_session_rejects_a_language_the_avatar_does_not_speak():
    with _client() as c:
        res = c.post("/sessions", json={"avatar": "igor", "language": "fr"})
        assert res.status_code == 422
        assert "does not speak 'fr'" in res.json()["detail"]
        assert c.post("/sessions", json={"avatar": "igor", "language": "en"}).status_code == 200


def test_offer_requires_a_valid_token():
    with _client() as c:
        sid = c.post("/sessions").json()["session_id"]
        offer = {"sdp": "v=0", "type": "offer"}
        assert c.post(f"/sessions/{sid}/offer", json=offer).status_code == 401
        assert c.post(f"/sessions/{sid}/offer?token=wrong", json=offer).status_code == 401
        assert c.post("/sessions/nope/offer?token=x", json=offer).status_code == 401


def test_offer_without_configured_services_is_503_not_a_crash(monkeypatch):
    from tv_avatar import app as app_module
    monkeypatch.setattr(app_module, "_missing_settings", lambda: ["NEBIUS_API_KEY"])
    with _client() as c:
        body = c.post("/sessions").json()
        res = c.post(
            f"/sessions/{body['session_id']}/offer?token={body['control_token']}",
            json={"sdp": "v=0", "type": "offer"},
        )
        assert res.status_code == 503


def test_config_reports_stack_without_secrets(monkeypatch):
    from tv_avatar import app as app_module
    monkeypatch.setattr(app_module, "_missing_settings", lambda: ["NEBIUS_API_KEY"])
    with _client() as c:
        body = c.get("/config").json()
        assert body["configured"] is False
        assert body["missing"] == ["NEBIUS_API_KEY"]
        assert body["llm_model"] == "Qwen/Qwen3-30B-A3B-Instruct-2507"
        assert body["tts_model"] == "cartesia/sonic:3"
        assert not any("key" in k for k in body)
        # Catalog is listed even without provider keys, minus provider ids.
        assert [l["code"] for l in body["languages"]] == ["en", "es", "fr", "ca"]
        assert body["default_avatar"] in {a["id"] for a in body["avatars"]}
        for a in body["avatars"]:
            assert "anam_avatar_id" not in a and "voice" not in a


def test_demo_console_is_served():
    with _client() as c:
        assert c.get("/", follow_redirects=False).headers["location"] == "/demo/"
        page = c.get("/demo/")
        assert page.status_code == 200
        assert "tv-avatar" in page.text


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


# --- lifecycle (review item 3) ----------------------------------------------

import time


def test_control_socket_drop_keeps_session_alive_for_reconnect():
    """Spec §6: a control-socket blip is Degraded, not Closing."""
    app = create_app()
    with TestClient(app) as c:
        body = c.post("/sessions").json()
        sid = body["session_id"]
        url = f"/sessions/{sid}/control?token={body['control_token']}"
        with c.websocket_connect(url) as ws:
            ws.receive_json()
        # socket gone; session and bus must both still exist
        assert app.state.store.get(sid) is not None
        assert app.state.manager.has(sid)
        with c.websocket_connect(url) as ws:
            assert ws.receive_json()["type"] == "agent_status"


def test_sweep_reaps_expired_sessions_and_their_buses():
    app = create_app()
    with TestClient(app) as c:
        live = c.post("/sessions").json()["session_id"]
        stale = c.post("/sessions").json()["session_id"]
        app.state.store.get(stale).expires_at = time.time() - 1

        swept = app.state.sweep()

        assert swept == [stale]
        assert app.state.store.get(stale) is None
        assert not app.state.manager.has(stale)
        assert app.state.store.get(live) is not None
        assert app.state.manager.has(live)


def test_delete_session_requires_token_and_then_closes_everything():
    app = create_app()
    with TestClient(app) as c:
        body = c.post("/sessions").json()
        sid, tok = body["session_id"], body["control_token"]

        assert c.delete(f"/sessions/{sid}").status_code == 401
        assert c.delete(f"/sessions/{sid}", headers={"X-Control-Token": "nope"}).status_code == 401
        assert c.delete(f"/sessions/{sid}", headers={"X-Control-Token": tok}).status_code == 204

        assert app.state.store.get(sid) is None
        assert not app.state.manager.has(sid)
        with c.websocket_connect(f"/sessions/{sid}/control?token={tok}") as ws:
            assert ws.receive_json()["code"] == "unauthorized"


def test_command_result_over_the_socket_is_a_linked_event_span(otel):
    """The TV's reply closes the loop: a `tv.command_result` event linked to the
    `tv.command` it answers, carrying the session id from the channel's own scope."""
    from tv_avatar.config import Settings
    from tv_avatar.control import channel as channel_module

    settings = Settings(_env_file=None, slng_api_key="s", anam_api_key="a", nebius_api_key="n")
    app = create_app()
    with TestClient(app) as c:
        body = c.post("/sessions", json={"user_id": "couch_3"}).json()
        bus = app.state.manager.bus_for(body["session_id"])
        url = f"/sessions/{body['session_id']}/control?token={body['control_token']}"
        original = channel_module.ControlChannel.__init__

        def with_settings(self, *a, **kw):        # create_app() runs without a .env
            kw["settings"] = settings
            original(self, *a, **kw)

        channel_module.ControlChannel.__init__ = with_settings
        try:
            with c.websocket_connect(url) as ws:
                ws.receive_json()  # agent_status
                c.portal.call(bus.dispatch, "home", {}, "turn_1")
                cmd = ws.receive_json()
                ws.send_text(json.dumps({"v": 1, "type": "ack", "command_id": cmd["id"], "ok": False,
                                         "error": "no such view"}))
                ws.send_text(json.dumps({"v": 1, "type": "ack", "command_id": "cmd_unknown", "ok": True}))
                ws.send_text(json.dumps({"v": 99}))
                assert ws.receive_json()["code"] == "unsupported_version"   # the reader has drained
        finally:
            channel_module.ControlChannel.__init__ = original
    spans = otel.spans()
    command, = spans["tv.command"]
    result, = spans["tv.command_result"]                              # the unknown id made none
    assert result.attributes["langfuse.observation.type"] == "event"
    assert result.attributes["langfuse.observation.metadata.status"] == "failed"
    assert result.attributes["tv.command.id"] == cmd["id"]
    assert result.attributes["tv.command.roundtrip_ms"] >= 0
    assert result.attributes["langfuse.session.id"] == body["session_id"]
    assert result.attributes["langfuse.user.id"] == "couch_3"
    assert result.parent is None
    assert result.links[0].context.span_id == command.context.span_id
    assert result.status.status_code.name == "ERROR"
