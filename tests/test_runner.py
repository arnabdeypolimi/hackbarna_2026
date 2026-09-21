"""The pipeline runner consolidates memory on every way a session can end."""
import asyncio
from types import SimpleNamespace

import pytest
from pipecat.frames.frames import TranscriptionFrame

from tv_avatar.config import Settings
from tv_avatar.control.bus import CommandBus
from tv_avatar.memory.fake import FakeMemoryLane
from tv_avatar.pipeline import runner
from tv_avatar.session.manager import SessionManager
from tv_avatar.session.state import SessionState
from tv_avatar.tracing import observation

SETTINGS = Settings(_env_file=None, slng_api_key="s", anam_api_key="a", nebius_api_key="n")


class _Runner:
    """Stands in for PipelineRunner: finishes, raises, or hangs until cancelled."""

    def __init__(self, behaviour: str) -> None:
        self._behaviour = behaviour

    async def run(self, _task) -> None:
        if self._behaviour == "raise":
            raise RuntimeError("transport died")
        if self._behaviour == "hang":
            await asyncio.Event().wait()


@pytest.mark.parametrize("behaviour", ["finish", "raise", "hang"])
async def test_finish_session_fires_however_the_pipeline_ends(monkeypatch, behaviour):
    lane = FakeMemoryLane()
    monkeypatch.setattr(runner, "build_pipeline", lambda *a, **k: object())
    monkeypatch.setattr(runner, "PipelineRunner", lambda handle_sigint: _Runner(behaviour))
    session = SessionState("sess_1", "tok", 0, user_id="couch_9")
    rt = SimpleNamespace(lane=lane)

    task = asyncio.create_task(
        runner.run_session(session, CommandBus(), None, runtime=rt, settings=SETTINGS))
    await asyncio.sleep(0.01)
    if behaviour == "hang":
        task.cancel()
    try:
        await task
    except (asyncio.CancelledError, RuntimeError):
        pass
    await asyncio.sleep(0.01)  # the consolidation runs on its own task
    assert lane.finished == ["couch_9"]


class _Task:
    """Stands in for PipelineTask: records what was queued into the pipeline."""

    def __init__(self) -> None:
        self.frames: list = []

    async def queue_frames(self, frames) -> None:
        self.frames.extend(frames)


async def test_typed_words_enter_the_pipeline_where_stt_would_have_put_them(monkeypatch):
    """A viewer utterance that never was audio still arrives as a TranscriptionFrame,
    so turn-taking, barge-in and the agent see the ordinary path."""
    pipeline = _Task()
    monkeypatch.setattr(runner, "build_pipeline", lambda *a, **k: pipeline)
    monkeypatch.setattr(runner, "PipelineRunner", lambda handle_sigint: _Runner("hang"))
    session = SessionState("sess_2", "tok", 0, user_id="couch_2")
    manager = SessionManager()

    task = asyncio.create_task(runner.run_session(
        session, CommandBus(), None, runtime=None, settings=SETTINGS,
        on_injector=manager.injector_slot("sess_2")))
    await asyncio.sleep(0.01)

    inject = manager.injector_for("sess_2")
    assert inject is not None, "a running pipeline must be reachable by typed text"
    await inject("I want to watch Barbie")
    frame, = pipeline.frames
    assert isinstance(frame, TranscriptionFrame)
    assert (frame.text, frame.user_id) == ("I want to watch Barbie", "couch_2")

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Nothing to say the words to once the pipeline is gone.
    assert manager.injector_for("sess_2") is None


async def test_a_replaced_pipelines_teardown_does_not_silence_its_successor(monkeypatch):
    """Teardown is not ordered against startup: the old pipeline reaches its
    `finally` after the new one has published, and must leave it alone."""
    monkeypatch.setattr(runner, "build_pipeline", lambda *a, **k: _Task())
    monkeypatch.setattr(runner, "PipelineRunner", lambda handle_sigint: _Runner("hang"))
    session = SessionState("sess_3", "tok", 0, user_id="couch_3")
    manager = SessionManager()

    old = asyncio.create_task(runner.run_session(
        session, CommandBus(), None, settings=SETTINGS,
        on_injector=manager.injector_slot("sess_3")))
    await asyncio.sleep(0.01)
    new = asyncio.create_task(runner.run_session(
        session, CommandBus(), None, settings=SETTINGS,
        on_injector=manager.injector_slot("sess_3")))
    await asyncio.sleep(0.01)
    live = manager.injector_for("sess_3")

    old.cancel()
    with pytest.raises(asyncio.CancelledError):
        await old
    assert manager.injector_for("sess_3") is live

    new.cancel()
    with pytest.raises(asyncio.CancelledError):
        await new


async def test_session_identity_reaches_spans_inside_the_pipeline_and_the_memory_fold(
        monkeypatch, otel):
    """Baggage from `run_session` lands on a span opened by pipeline code and on
    the session-end consolidation task that outlives the pipeline (D20)."""
    class _TracedRunner:
        async def run(self, _task) -> None:
            with observation("inside-pipeline", type="span"):
                pass

    class _Lane(FakeMemoryLane):
        async def finish_session(self, user_id: str) -> None:
            with observation("memory.finish_session", type="generation"):
                await super().finish_session(user_id)

    monkeypatch.setattr(runner, "build_pipeline", lambda *a, **k: object())
    monkeypatch.setattr(runner, "PipelineRunner", lambda handle_sigint: _TracedRunner())
    session = SessionState("sess_7", "tok", 0, user_id="couch_7")
    await runner.run_session(session, CommandBus(), None, runtime=SimpleNamespace(lane=_Lane()),
                             settings=SETTINGS)
    await asyncio.sleep(0.01)
    spans = otel.spans()
    for name in ("inside-pipeline", "memory.finish_session"):
        s, = spans[name]
        assert s.attributes["langfuse.session.id"] == "sess_7"
        assert s.attributes["langfuse.user.id"] == "couch_7"
