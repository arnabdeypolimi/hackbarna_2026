"""Run one session's pipeline to completion."""
import asyncio

from loguru import logger
from pipecat.pipeline.runner import PipelineRunner

from tv_avatar.control.bus import CommandBus
from tv_avatar.pipeline.builder import build_pipeline
from tv_avatar.runtime import Runtime
from tv_avatar.session.state import SessionState


async def run_session(
    session: SessionState,
    bus: CommandBus,
    transport,
    *,
    with_avatar: bool = True,
    half_duplex: bool = False,
    runtime: Runtime | None = None,
) -> None:
    task = build_pipeline(
        transport, session, bus, with_avatar=with_avatar, half_duplex=half_duplex,
        runtime=runtime,
    )
    try:
        await PipelineRunner(handle_sigint=False).run(task)
    finally:
        # Every way a session ends — hang-up, DELETE, replacement by a new offer,
        # shutdown cancel — passes here. The memory lane consolidates the session
        # off this task so a slow LLM never delays the teardown.
        if runtime is not None:
            user_id = session.user_id or session.session_id
            consolidate = asyncio.create_task(runtime.lane.finish_session(user_id),
                                              name=f"memory-finish:{session.session_id}")
            consolidate.add_done_callback(_log_failure)


def _log_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.opt(exception=task.exception()).warning("memory finish_session failed")
