"""The weather agent's fal proxy: what it relays, refuses and counts."""
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from tv_avatar.theme_agent.app import (
    MAX_BODY_BYTES,
    SessionBudget,
    create_theme_agent_app,
)
from tv_avatar.theme_agent.settings import ThemeAgentSettings

ENDPOINT = "minimax/h3-max/director"
SESSION = "https://wma.fal.run/session"
ORIGIN = "http://localhost:5173"


class Upstream:
    """Stands in for fal's bridge and remembers what it was asked."""

    def __init__(self, status: int = 200, body: dict | None = None) -> None:
        self.status = status
        self.body = body if body is not None else {"session_id": "s1", "sdp": "answer"}
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        # Headers fal might send, so the "not relayed" test has something to catch.
        return httpx.Response(self.status, json=self.body, headers={
            "set-cookie": "sid=1", "x-fal-internal": "1"})


def _client(upstream=None, *, key: str = "k-test", budget=None, **kw) -> TestClient:
    upstream = upstream or Upstream()
    settings = ThemeAgentSettings(_env_file=None, fal_key=key, **kw)
    http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    return TestClient(create_theme_agent_app(settings, http, budget))


def _post(c: TestClient, target: str = SESSION, body: dict | None = None, **kw):
    if body is None:
        body = {"app_id": ENDPOINT, "sdp": "offer", "type": "offer"}
    return c.post("/fal/proxy", json=body, headers={"x-fal-target-url": target}, **kw)


def test_a_session_offer_is_relayed_with_the_key_added_and_the_answer_returned():
    upstream = Upstream()
    with _client(upstream) as c:
        r = _post(c)
    assert r.status_code == 200
    assert r.json() == {"session_id": "s1", "sdp": "answer"}
    (call,) = upstream.calls
    assert str(call.url) == SESSION
    assert call.headers["authorization"] == "Key k-test"
    assert b'"sdp":"offer"' in call.content.replace(b" ", b"")


def test_neither_the_key_nor_fals_headers_reach_the_browser():
    with _client() as c:
        r = _post(c)
    assert "k-test" not in r.text
    assert "set-cookie" not in r.headers
    assert "x-fal-internal" not in r.headers


@pytest.mark.parametrize("target", [
    "https://fal.run/minimax/h3-max/director",   # another fal host
    "https://wma.fal.run.evil.example/session",  # only starts like the bridge
    "https://wma.fal.run@evil.example/session",  # the bridge as mere userinfo
    "http://wma.fal.run/session",                # not https
    "https://wma.fal.run/admin",                 # a path the SDK never calls
    "https://wma.fal.run/session?x=1",           # nor with a query
    "",                                          # no target at all
])
def test_a_target_other_than_the_bridge_is_refused_before_the_key_leaves(target):
    upstream = Upstream()
    with _client(upstream) as c:
        r = _post(c, target)
    assert r.status_code == 403
    assert upstream.calls == []


@pytest.mark.parametrize("path", ["/ice", "/session"])
def test_only_the_configured_model_may_be_opened(path):
    upstream = Upstream()
    with _client(upstream) as c:
        r = _post(c, f"https://wma.fal.run{path}", {"app_id": "fal-ai/flux/dev"})
    assert r.status_code == 403
    assert upstream.calls == []


def test_a_heartbeat_names_no_model_and_still_passes():
    upstream = Upstream()
    with _client(upstream) as c:
        r = _post(c, "https://wma.fal.run/session/heartbeat", {"session_id": "s1"})
    assert r.status_code == 200
    assert len(upstream.calls) == 1


@pytest.mark.parametrize("raw", [b"not json", b"[1, 2]", b'"text"'])
def test_a_body_that_is_not_a_json_object_is_refused(raw):
    with _client() as c:
        r = c.post("/fal/proxy", content=raw, headers={"x-fal-target-url": SESSION})
    assert r.status_code == 400


def test_an_oversized_body_is_refused():
    with _client() as c:
        r = _post(c, body={"app_id": ENDPOINT, "sdp": "x" * (MAX_BODY_BYTES + 1)})
    assert r.status_code == 413


def test_with_no_key_it_still_starts_and_says_so():
    upstream = Upstream()
    with _client(upstream, key="") as c:
        assert c.get("/health").json()["configured"] is False
        r = _post(c)
    assert r.status_code == 503
    assert upstream.calls == []


def test_sessions_stop_at_the_daily_cap_but_ice_and_heartbeats_do_not_count():
    upstream = Upstream()
    with _client(upstream, theme_agent_max_sessions_per_day=2) as c:
        assert _post(c).status_code == 200
        assert _post(c, "https://wma.fal.run/ice").status_code == 200
        assert _post(c, "https://wma.fal.run/session/heartbeat", {"session_id": "s"}
                     ).status_code == 200
        assert _post(c).status_code == 200
        third = _post(c)
        health = c.get("/health").json()
    assert third.status_code == 429
    assert health["sessions_left_today"] == 0
    # Two sessions, one ice, one heartbeat: the refused third never reached fal.
    assert len(upstream.calls) == 4


def test_a_session_fal_refused_to_open_is_not_charged_to_the_day():
    upstream = Upstream(status=500, body={"detail": "no runner"})
    with _client(upstream, theme_agent_max_sessions_per_day=1) as c:
        first = _post(c)
        second = _post(c)
    assert first.status_code == second.status_code == 500  # relayed, not a 429


def test_fal_being_unreachable_is_a_502_and_is_not_charged_to_the_day():
    def down(request):
        raise httpx.ConnectError("nope")

    settings = ThemeAgentSettings(
        _env_file=None, fal_key="k", theme_agent_max_sessions_per_day=1)
    http = httpx.AsyncClient(transport=httpx.MockTransport(down))
    with TestClient(create_theme_agent_app(settings, http)) as c:
        assert _post(c).status_code == 502
        # A 429 here would mean the first attempt had been charged to the day.
        assert _post(c).status_code == 502


#: A real key is <uuid>:<secret>. Typing a quote on some keyboard layouts
#: (US-International and others) gives a dead-key diaeresis (U+00A8) instead,
#: and .env keeps it in the value.
DIAERESIS = chr(0xA8)
WRAPPED = f"{DIAERESIS}abc-def:0123{DIAERESIS}"  # 14 characters: stray ones at 0 and 13


def test_a_key_with_stray_characters_is_refused_cleanly_not_crashed():
    """The bug: HTTP headers are ASCII, so the Authorization header could not be built
    and the route died with an unhandled 500, which the browser sees as a bare
    "Failed to fetch" because a crash carries no CORS headers."""
    upstream = Upstream()
    with _client(upstream, key=WRAPPED) as c:
        health = c.get("/health").json()
        r = _post(c)
    assert r.status_code == 503
    assert "U+00A8 at position 0" in r.json()["detail"]
    assert "U+00A8 at position 13" in r.json()["detail"]
    assert "abc-def" not in r.text and "abc-def" not in str(health)  # never echoed back
    assert health["configured"] is False
    assert "U+00A8" in health["problem"]
    assert upstream.calls == []


@pytest.mark.parametrize("key", ["has a space", "tab\tinside", "trailing\n"])
def test_whitespace_in_a_key_is_refused_the_same_way(key):
    with _client(key=key) as c:
        assert _post(c).status_code == 503
        assert c.get("/health").json()["configured"] is False


def test_a_good_key_reports_no_problem():
    with _client() as c:
        health = c.get("/health").json()
    assert health["configured"] is True
    assert health["problem"] is None


@pytest.mark.parametrize("failure", [
    httpx.ConnectError("nope"),
    RuntimeError("something nobody planned for"),
])
def test_any_failure_to_forward_is_a_502_and_is_not_charged_to_the_day(failure):
    def down(request):
        raise failure

    settings = ThemeAgentSettings(
        _env_file=None, fal_key="k", theme_agent_max_sessions_per_day=1)
    http = httpx.AsyncClient(transport=httpx.MockTransport(down))
    with TestClient(create_theme_agent_app(settings, http)) as c:
        assert _post(c).status_code == 502
        assert _post(c).status_code == 502  # a 429 would mean the first was charged
        assert c.get("/health").json()["sessions_left_today"] == 1


def test_the_cap_starts_over_on_a_new_utc_day():
    day = [date(2026, 9, 20)]
    budget = SessionBudget(1, today=lambda: day[0])
    assert budget.take() is True
    assert budget.take() is False
    day[0] = date(2026, 9, 21)
    assert budget.take() is True


def _allowed(c: TestClient, origin: str) -> str | None:
    """The origin a browser preflight would be told it may use, or None if refused."""
    r = c.options("/fal/proxy", headers={
        "Origin": origin,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,x-fal-target-url",
    })
    return r.headers.get("access-control-allow-origin")


def test_preflight_lets_a_page_send_the_target_header():
    with _client() as c:
        r = c.options("/fal/proxy", headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-fal-target-url",
        })
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == ORIGIN
    assert "x-fal-target-url" in r.headers["access-control-allow-headers"].lower()


@pytest.mark.parametrize("origin", [
    "http://localhost:5173",
    "http://localhost:5176",    # Vite moved on because 5173 was taken
    "http://127.0.0.1:5199",
    "http://[::1]:5173",
    "http://localhost",         # no port at all
])
def test_localhost_is_allowed_on_any_port(origin):
    with _client() as c:
        assert _allowed(c, origin) == origin


@pytest.mark.parametrize("origin", [
    "https://elsewhere.example",
    "http://localhost.evil.example",         # starts like localhost
    "http://localhost:5173.evil.example",
    "https://127.0.0.1.evil.example",
    "http://evil.example:5173",
    "http://172.20.10.12:5173",              # a LAN address is not loopback
])
def test_nothing_else_is_allowed_unless_it_is_listed(origin):
    with _client() as c:
        assert _allowed(c, origin) is None


def test_a_listed_origin_is_allowed_beside_localhost():
    listed = "http://172.20.10.12:5173, https://tv.example"
    with _client(theme_agent_origins=listed) as c:
        assert _allowed(c, "http://172.20.10.12:5173") == "http://172.20.10.12:5173"
        assert _allowed(c, "https://tv.example") == "https://tv.example"
        assert _allowed(c, "http://localhost:5176") == "http://localhost:5176"


def test_localhost_can_be_switched_off_for_a_deployed_agent():
    with _client(theme_agent_allow_localhost=False,
                 theme_agent_origins="https://tv.example") as c:
        assert _allowed(c, "http://localhost:5173") is None
        assert _allowed(c, "https://tv.example") == "https://tv.example"
