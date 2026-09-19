"""Assemble the Pipecat pipeline (spec §4).

Only two elements are ours: the agent and the events observer.
Everything else is library code wired from configuration.
"""
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.services.llm_service import LLMService

from tv_avatar.agent.llm import build_llm
from tv_avatar.agent.prompt import initial_messages
from tv_avatar.config import get_settings
from tv_avatar.control.bus import CommandBus
from tv_avatar.pipeline.observers import SessionEventsObserver
from tv_avatar.pipeline.services import build_anam, build_stt, build_tts
from tv_avatar.pipeline.turns import user_aggregator_params
from tv_avatar.session.state import SessionState


def build_pipeline(
    transport,
    session: SessionState,
    bus: CommandBus,
    *,
    with_avatar: bool = True,
    llm: LLMService | None = None,
    greet: bool = True,
    half_duplex: bool = False,
) -> PipelineTask:
    """Build one session's task.

    ``llm`` lets tests substitute the scripted stub for the real provider.
    ``greet`` runs the LLM once on client connect so the avatar opens the
    conversation instead of waiting in silence. ``half_duplex`` mutes the
    mic while the avatar speaks (diagnostic; disables barge-in).
    """
    settings = get_settings()
    avatar, language = session.persona.avatar, session.persona.language
    context = LLMContext(messages=initial_messages(language))
    user_agg, assistant_agg = LLMContextAggregatorPair(
        context,
        user_params=user_aggregator_params(
            turn_silence_s=settings.turn_silence_s, half_duplex=half_duplex,
        ),
    )

    # TODO(phase 2): insert ScreenContextInjector between user_agg and the LLM
    # so session.render_for_prompt() is injected fresh on every run (spec §4).
    stages = [
        transport.input(),
        build_stt(settings, language.pipecat),
        user_agg,
        llm or build_llm(settings),
        build_tts(settings, avatar.voice, language.pipecat),
    ]
    if with_avatar:
        stages.append(build_anam(settings, avatar))
    stages += [transport.output(), assistant_agg]

    task = PipelineTask(
        Pipeline(stages),
        params=PipelineParams(enable_metrics=True),
        observers=[SessionEventsObserver(bus)],
    )

    if greet:
        @transport.event_handler("on_client_connected")
        async def _on_connected(_transport, _client) -> None:
            await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def _on_disconnected(_transport, _client) -> None:
        await task.cancel()

    return task
