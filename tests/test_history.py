from tv_avatar.control.protocol import Playback, ScreenState, Tile
from tv_avatar.history.recorder import HistoryRecorder, transition_events
from tv_avatar.history.store import Event, EventKind, HistoryStore


def _screen(state: str, title_id: str | None = None, pos: float = 0.0) -> ScreenState:
    return ScreenState(
        view="player" if state != "stopped" else "grid",
        tiles=[Tile(title_id="27205", name="Inception", position=0)],
        playback=Playback(state=state, title_id=title_id, position_s=pos),
    )


async def test_watched_ids_and_recents(tmp_path):
    store = HistoryStore(str(tmp_path / "h.db"))
    await store.record(Event(user_id="u1", kind=EventKind.PLAY_STARTED, title_id="27205"))
    await store.record(Event(user_id="u2", kind=EventKind.PLAY_STARTED, title_id="680"))
    assert await store.watched_ids("u1") == {"27205"}
    assert await store.watched_ids("u2") == {"680"}
    assert await store.recent_titles("u1") == ["27205"]
    await store.close()


async def test_engaged_ids_need_completion_or_long_watch(tmp_path):
    store = HistoryStore(str(tmp_path / "h.db"))
    await store.record(Event(user_id="u1", kind=EventKind.PLAY_COMPLETED, title_id="1"))
    await store.record(Event(user_id="u1", kind=EventKind.PLAY_ABANDONED, title_id="2",
                             detail={"watched_s": 900}))
    await store.record(Event(user_id="u1", kind=EventKind.PLAY_ABANDONED, title_id="3",
                             detail={"watched_s": 30}))
    assert await store.engaged_ids("u1") == ["2", "1"]
    await store.close()


def test_stopped_to_playing_records_play_started():
    events = transition_events(_screen("stopped"), _screen("playing", "27205"), runtime_s=None)
    assert [e.kind for e in events] == [EventKind.PLAY_STARTED]
    assert events[0].title_id == "27205"


def test_playing_to_stopped_near_end_is_completed():
    old = _screen("playing", "27205", pos=8000)
    events = transition_events(old, _screen("stopped"), runtime_s=148 * 60)
    assert [e.kind for e in events] == [EventKind.PLAY_COMPLETED]


def test_playing_to_stopped_early_is_abandoned():
    old = _screen("playing", "27205", pos=120)
    events = transition_events(old, _screen("stopped"), runtime_s=148 * 60)
    assert [e.kind for e in events] == [EventKind.PLAY_ABANDONED]
    assert events[0].detail["watched_s"] == 120


def test_pause_is_not_a_transition_event():
    assert transition_events(_screen("playing", "1", 10), _screen("paused", "1", 10), runtime_s=None) == []


async def test_recorder_renders_recent_names(tmp_path):
    class Cat:
        def lookup(self, title_id):
            from tv_avatar.recs.catalog import CatalogItem
            return CatalogItem(title_id=title_id, name="Inception", year=2010, runtime=148) if title_id == "27205" else None

    store = HistoryStore(str(tmp_path / "h.db"))
    rec = HistoryRecorder(store, Cat())
    await rec.on_screen_transition("u1", _screen("stopped"), _screen("playing", "27205"))
    text = await store.render_for_prompt("u1", catalog=Cat())
    assert "Inception" in text
    assert "(none yet)" in await store.render_for_prompt("nobody", catalog=Cat())
    await store.close()
