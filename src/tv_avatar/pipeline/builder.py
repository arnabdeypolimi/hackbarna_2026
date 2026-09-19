"""Assemble the Pipecat pipeline (spec §4, phase-2 frame graph).

Pipecat is the only orchestrator; SLNG owns speech I/O (D12). Library
services are wired from configuration; everything of ours is a
FrameProcessor or an observer inserted at a fixed position:

    input → STT → MemoryPrefetchTap → user_agg → [ScreenContextInjector, stub only] →
    agent → TTS → [Anam] → output → assistant_agg → [MemoryIngestTap, non-sgr only]

The SGR agent writes its own system prompt and ingests memory itself, so
neither bracketed processor is in its graph.
"""
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
)
from pipecat.services.llm_service import LLMService

from tv_avatar.agent.injector import ScreenContextInjector
from tv_avatar.agent.llm import StubLLMService, build_llm
from tv_avatar.agent.prompt import initial_messages
from tv_avatar.config import Settings, get_settings
from tv_avatar.control.bus import CommandBus
from tv_avatar.memory.taps import MemoryIngestTap, MemoryPrefetchTap
from tv_avatar.pipeline.observers import SessionEventsObserver, TurnLatencyObserver
from tv_avatar.pipeline.services import build_anam, build_stt, build_tts
from tv_avatar.pipeline.turns import user_aggregator_params
from tv_avatar.runtime import Runtime, build_runtime
from tv_avatar.session.state import SessionState


def build_agent(settings: Settings, runtime: Runtime, session: SessionState, bus: CommandBus) -> LLMService:
    """The AGENT_IMPL switch: `sgr` (phase 2), `chat` (plain LLM), `stub` (tests)."""
    match settings.agent_impl:
        case "sgr":
            from tv_avatar.agent.service import SGRAgentService
            from tv_avatar.agent.tools import InternalTools

            tools = InternalTools(runtime.recs, runtime.lane, runtime.catalog,
                                  recorder=runtime.recorder, timeout_s=settings.tool_timeout_s)
            return SGRAgentService(settings, bus, runtime.lane, runtime.recs, runtime.history, session,
                                   catalog=runtime.catalog, tools=tools)
        case "chat":
            return build_llm(settings)
        case _:
            return StubLLMService(bus=bus, session=session)


def build_pipeline(
    transport,
    session: SessionState,
    bus: CommandBus,
    *,
    with_avatar: bool = True,
    llm: LLMService | None = None,
    greet: bool = True,
    half_duplex: bool = False,
    runtime: Runtime | None = None,
    settings: Settings | None = None,
) -> PipelineTask:
    """Build one session's task.

    ``llm`` lets tests substitute the scripted stub for the real provider.
    ``greet`` runs the LLM once on client connect so the avatar opens the
    conversation instead of waiting in silence. ``half_duplex`` mutes the
    mic while the avatar speaks (diagnostic; disables barge-in).
    """
    settings = settings or get_settings()
    runtime = runtime or build_runtime(settings)
    avatar, language = session.persona.avatar, session.persona.language
    context = LLMContext(messages=initial_messages(language))
    user_agg, assistant_agg = LLMContextAggregatorPair(
        context,
        user_params=user_aggregator_params(
            turn_silence_s=settings.turn_silence_s, half_duplex=half_duplex,
        ),
    )
    agent = llm or build_agent(settings, runtime, session, bus)

    stages = [
        transport.input(),
        build_stt(settings, language.pipecat),
        MemoryPrefetchTap(runtime.lane, session, min_chars=settings.mem_prefetch_min_chars, recs=runtime.recs),
        user_agg,
    ]
    if settings.agent_impl == "stub":
        # The SGR agent writes its own system prompt each turn; the plain chat
        # LLM keeps SYSTEM_PROMPT as-is. Only the stub, which exercises the same
        # graph in tests without an agent, needs the sections stamped for it.
        stages.append(ScreenContextInjector(session, catalog=runtime.catalog, history=runtime.history))
    stages += [agent, build_tts(settings, avatar.voice, language.pipecat_tts)]
    if with_avatar:
        stages.append(build_anam(settings, avatar))
    stages += [transport.output(), assistant_agg]
    if settings.agent_impl != "sgr":
        # The SGR agent ingests at turn end / interruption itself (it knows the
        # user text and what was actually said); the frame tap serves the other
        # implementations. Frames reach this point only after playback, so an
        # interrupted reply would otherwise never be remembered.
        stages.append(MemoryIngestTap(runtime.lane, session, context))

    task = PipelineTask(
        Pipeline(stages),
        params=PipelineParams(enable_metrics=True),
        observers=[SessionEventsObserver(bus), TurnLatencyObserver(session)],
    )

    if greet:
        @transport.event_handler("on_client_connected")
        async def _on_connected(_transport, _client) -> None:
            await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def _on_disconnected(_transport, _client) -> None:
        await task.cancel()

    return task
