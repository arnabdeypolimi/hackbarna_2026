"""The pipeline runner consolidates memory on every way a session can end."""
import asyncio
from types import SimpleNamespace

import pytest

from tv_avatar.config import Settings
from tv_avatar.control.bus import CommandBus
from tv_avatar.memory.fake import FakeMemoryLane
from tv_avatar.pipeline import runner
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
