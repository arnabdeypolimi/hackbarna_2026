"""The pipeline runner consolidates memory on every way a session can end."""
import asyncio
from types import SimpleNamespace

import pytest

from tv_avatar.control.bus import CommandBus
from tv_avatar.memory.fake import FakeMemoryLane
from tv_avatar.pipeline import runner
from tv_avatar.session.state import SessionState


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

    task = asyncio.create_task(runner.run_session(session, CommandBus(), None, runtime=rt))
    await asyncio.sleep(0.01)
    if behaviour == "hang":
        task.cancel()
    try:
        await task
    except (asyncio.CancelledError, RuntimeError):
        pass
    await asyncio.sleep(0.01)  # the consolidation runs on its own task
    assert lane.finished == ["couch_9"]
