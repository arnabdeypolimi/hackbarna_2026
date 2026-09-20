import pytest

from conftest import PERSONA
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
    a, b = store.create(60, PERSONA), store.create(60, PERSONA)
    assert a.session_id != b.session_id
    assert a.control_token != b.control_token


def test_authenticate_accepts_matching_token():
    store = SessionStore()
    s = store.create(60, PERSONA)
    assert store.authenticate(s.session_id, s.control_token) is s


def test_authenticate_rejects_wrong_token():
    store = SessionStore()
    s = store.create(60, PERSONA)
    with pytest.raises(PermissionError):
        store.authenticate(s.session_id, "not-the-token")


def test_authenticate_rejects_unknown_session():
    with pytest.raises(KeyError):
        SessionStore().authenticate("nope", "tok")


def test_latest_screen_state_wins():
    store = SessionStore()
    s = store.create(60, PERSONA)
    s.update_screen(_state())
    later = _state()
    later.focus_index = 0
    s.update_screen(later)
    assert s.screen.focus_index == 0


def test_render_for_prompt_mentions_focused_tile():
    store = SessionStore()
    s = store.create(60, PERSONA)
    s.update_screen(_state())
    rendered = s.render_for_prompt()
    assert "Sicario" in rendered
    assert "focused" in rendered.lower()


def test_render_for_prompt_marks_shoppable_tiles_only():
    s = SessionStore().create(60, PERSONA)
    state = _state()
    state.tiles[1].shoppable = True
    s.update_screen(state)
    heat, sicario = [ln for ln in s.render_for_prompt().splitlines() if ln.startswith("  [")]
    assert "[shop]" not in heat
    assert "[shop]" in sicario


def test_tile_defaults_to_not_shoppable():
    # Older TV apps do not send the field; they must still validate.
    assert Tile(title_id="x", name="X", position=0).shoppable is False


def test_render_for_prompt_handles_no_state_yet():
    s = SessionStore().create(60, PERSONA)
    assert "unknown" in s.render_for_prompt().lower()


def test_new_turn_changes_the_turn_id():
    s = SessionStore().create(60, PERSONA)
    first = s.new_turn()
    assert s.current_turn_id == first
    assert s.new_turn() != first


def test_close_removes_the_session():
    store = SessionStore()
    s = store.create(60, PERSONA)
    store.close(s.session_id)
    assert store.get(s.session_id) is None


def test_sweep_expired_removes_only_past_expiry_sessions():
    store = SessionStore()
    fresh = store.create(3600, PERSONA)
    stale = store.create(3600, PERSONA)
    stale.expires_at = 100.0

    assert store.sweep_expired(now=200.0) == [stale.session_id]
    assert store.get(stale.session_id) is None
    assert store.get(fresh.session_id) is fresh
    assert store.sweep_expired(now=200.0) == []
