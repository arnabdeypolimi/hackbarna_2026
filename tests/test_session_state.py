import pytest

from tv_avatar.control.protocol import Playback, ScreenState, Tile
from tv_avatar.session.state import SessionStore


def _state():
    return ScreenState(
        view="grid",
        rail_id="rail_trending",
        focus_index=1,
        tiles=[
            Tile(title_id="tt_1", name="Heat", position=0),
            Tile(title_id="tt_2", name="Sicario", position=1),
        ],
        playback=Playback(state="stopped"),
    )


def test_create_returns_distinct_ids_and_tokens():
    store = SessionStore()
    a, b = store.create(60), store.create(60)
    assert a.session_id != b.session_id
    assert a.control_token != b.control_token


def test_authenticate_accepts_matching_token():
    store = SessionStore()
    s = store.create(60)
    assert store.authenticate(s.session_id, s.control_token) is s


def test_authenticate_rejects_wrong_token():
    store = SessionStore()
    s = store.create(60)
    with pytest.raises(PermissionError):
        store.authenticate(s.session_id, "not-the-token")


def test_authenticate_rejects_unknown_session():
    with pytest.raises(KeyError):
        SessionStore().authenticate("nope", "tok")


def test_latest_screen_state_wins():
    store = SessionStore()
    s = store.create(60)
    s.update_screen(_state())
    later = _state()
    later.focus_index = 0
    s.update_screen(later)
    assert s.screen.focus_index == 0


def test_render_for_prompt_mentions_focused_tile():
    store = SessionStore()
    s = store.create(60)
    s.update_screen(_state())
    rendered = s.render_for_prompt()
    assert "Sicario" in rendered
    assert "focused" in rendered.lower()


def test_render_for_prompt_handles_no_state_yet():
    s = SessionStore().create(60)
    assert "unknown" in s.render_for_prompt().lower()


def test_new_turn_changes_the_turn_id():
    s = SessionStore().create(60)
    first = s.new_turn()
    assert s.current_turn_id == first
    assert s.new_turn() != first


def test_close_removes_the_session():
    store = SessionStore()
    s = store.create(60)
    store.close(s.session_id)
    assert store.get(s.session_id) is None
