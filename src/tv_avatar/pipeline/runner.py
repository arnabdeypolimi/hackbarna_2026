"""Run one session's pipeline to completion."""
from pipecat.pipeline.runner import PipelineRunner

from tv_avatar.control.bus import CommandBus
from tv_avatar.pipeline.builder import build_pipeline
from tv_avatar.session.state import SessionState


async def run_session(
    session: SessionState,
    bus: CommandBus,
    transport,
    *,
    with_avatar: bool = True,
) -> None:
    task = build_pipeline(transport, session, bus, with_avatar=with_avatar)
    await PipelineRunner(handle_sigint=False).run(task)
