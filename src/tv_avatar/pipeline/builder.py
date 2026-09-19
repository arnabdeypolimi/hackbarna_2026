"""Assemble the Pipecat pipeline (spec §4).

Only two elements are ours: the screen-state injector and the agent.
Everything else is library code wired from configuration.
"""
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)

from tv_avatar.agent.llm import StubLLMService
from tv_avatar.config import get_settings
from tv_avatar.control.bus import CommandBus
from tv_avatar.pipeline.services import build_anam, build_stt, build_tts
from tv_avatar.session.state import SessionState


def build_pipeline(
    transport,
    session: SessionState,
    bus: CommandBus,
    *,
    with_avatar: bool = True,
) -> PipelineTask:
    settings = get_settings()
    context = LLMContext()
    user_agg, assistant_agg = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    stages = [
        transport.input(),
        build_stt(settings),
        user_agg,
        StubLLMService(bus=bus),
        build_tts(settings),
    ]
    if with_avatar:
        stages.append(build_anam(settings))
    stages += [transport.output(), assistant_agg]

    return PipelineTask(Pipeline(stages), params=PipelineParams(enable_metrics=True))
