"""Assemble the Pipecat pipeline (spec §4, phase-2 frame graph).

Pipecat is the only orchestrator; SLNG owns speech I/O (D12). Everything
phase 2 adds is a FrameProcessor or an observer inserted at a fixed position.
"""
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.llm_service import LLMService

from tv_avatar.agent.injector import ScreenContextInjector
from tv_avatar.agent.llm import StubLLMService
from tv_avatar.config import Settings, get_settings
from tv_avatar.control.bus import CommandBus
from tv_avatar.memory.taps import MemoryIngestTap, MemoryPrefetchTap
from tv_avatar.pipeline.observers import AgentStatusObserver, TurnLatencyObserver
from tv_avatar.pipeline.services import build_anam, build_stt, build_tts
from tv_avatar.runtime import Runtime, build_runtime
from tv_avatar.session.state import SessionState


def build_agent(settings: Settings, runtime: Runtime, session: SessionState, bus: CommandBus) -> LLMService:
    """The AGENT_IMPL switch: the stub stays the deterministic test double."""
    if settings.agent_impl == "sgr":
        from tv_avatar.agent.service import SGRAgentService
        from tv_avatar.agent.tools import InternalTools

        tools = InternalTools(runtime.recs, runtime.lane, runtime.catalog,
                              recorder=runtime.recorder, timeout_s=settings.tool_timeout_s)
        return SGRAgentService(settings, bus, runtime.lane, runtime.recs, runtime.history, session,
                               catalog=runtime.catalog, tools=tools)
    return StubLLMService(bus=bus)


def build_pipeline(
    transport,
    session: SessionState,
    bus: CommandBus,
    *,
    with_avatar: bool = True,
    runtime: Runtime | None = None,
    settings: Settings | None = None,
) -> PipelineTask:
    settings = settings or get_settings()
    runtime = runtime or build_runtime(settings)
    context = LLMContext()
    user_agg, assistant_agg = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    stages = [
        transport.input(),
        build_stt(settings),
        MemoryPrefetchTap(runtime.lane, session, min_chars=settings.mem_prefetch_min_chars, recs=runtime.recs),
        user_agg,
        ScreenContextInjector(session, catalog=runtime.catalog, history=runtime.history),
        build_agent(settings, runtime, session, bus),
        build_tts(settings),
    ]
    if with_avatar:
        stages.append(build_anam(settings))
    stages += [
        transport.output(),
        assistant_agg,
        MemoryIngestTap(runtime.lane, session, context),
    ]

    return PipelineTask(
        Pipeline(stages),
        params=PipelineParams(enable_metrics=True),
        observers=[AgentStatusObserver(bus), TurnLatencyObserver(session)],
    )
