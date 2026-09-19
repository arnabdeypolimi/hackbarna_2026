"""Placeholder agent. The real one is deferred (spec §14).

Deterministic by design: a scripted stub makes interruption and
frame-ordering assertions reproducible in a way a real model cannot.
This module keeps its value after phase 2 as the pipeline test double.

Base class path confirmed in Task 1, Step 6 against pipecat-ai 1.11.0:
    pipecat.services.llm_service.LLMService
No abstract methods; __init__(run_in_parallel=True, group_parallel_tools=True,
function_call_timeout_secs=None, enable_async_tool_cancellation=False,
settings=None, **kwargs).
"""
import uuid
from dataclasses import dataclass, field

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import LLMService
from pipecat.services.settings import LLMSettings

from tv_avatar.control.bus import CommandBus
from tv_avatar.session.state import SessionState


@dataclass
class ScriptedTurn:
    text: str
    commands: list[tuple[str, dict]] = field(default_factory=list)


DEFAULT_SCRIPT: list[ScriptedTurn] = [
    ScriptedTurn("Sure, putting that on now.", [("play", {"title_id": "tt_1"})]),
    ScriptedTurn("Paused.", [("pause", {})]),
    ScriptedTurn("Moving right.", [("navigate", {"direction": "right", "count": 1})]),
    ScriptedTurn("Here's what's on screen.", []),
]


def _stub_settings() -> LLMSettings:
    # LLMService validates that every settings field is initialised; the stub
    # supports none of the sampling knobs, so they are explicitly None.
    return LLMSettings(
        model="stub",
        system_instruction=None,
        temperature=None,
        max_tokens=None,
        top_p=None,
        top_k=None,
        frequency_penalty=None,
        presence_penalty=None,
        seed=None,
        filter_incomplete_user_turns=None,
        user_turn_completion_config=None,
    )


class StubLLMService(LLMService):
    """Emits fixed prose and fixed commands, ignoring its input entirely.

    Behaves as a real LLM stage in the pipeline: every ``LLMContextFrame``
    becomes ``LLMFullResponseStartFrame → LLMTextFrame → LLMFullResponseEndFrame``
    with the turn's commands dispatched on the bus *before* the text, so the
    UI reacts before speech completes (spec §5). An ``InterruptionFrame``
    cancels the current turn's queued-but-unsent commands (spec §9).
    """

    def __init__(
        self,
        bus: CommandBus,
        script: list[ScriptedTurn] | None = None,
        session: SessionState | None = None,
        **kwargs,
    ) -> None:
        kwargs.setdefault("settings", _stub_settings())
        super().__init__(**kwargs)
        self._bus = bus
        self._session = session
        self._script = script or DEFAULT_SCRIPT
        self._index = 0
        self._current_turn_id: str | None = None

    def next_turn(self) -> ScriptedTurn:
        turn = self._script[self._index % len(self._script)]
        self._index += 1
        return turn

    def _new_turn_id(self) -> str:
        if self._session is not None:
            return self._session.new_turn()
        return f"turn_{uuid.uuid4().hex[:8]}"

    def cancel_current_turn(self) -> int:
        """Barge-in: drop the current turn's queued-but-unsent commands (spec §9).

        Returns how many were dropped. Commands already sent are never
        rolled back; that is the bus's contract, not ours to override.
        """
        if self._current_turn_id is None:
            return 0
        dropped = self._bus.cancel_turn(self._current_turn_id)
        if dropped:
            logger.debug("barge-in dropped {} unsent command(s)", dropped)
        return dropped

    async def run_scripted_turn(self, turn_id: str) -> str:
        """Dispatch this turn's commands, then return its prose.

        Commands go first so the UI reacts before speech completes, which
        is the ordering the real agent must also honour (spec §5).
        """
        turn = self.next_turn()
        for verb, args in turn.commands:
            await self._bus.dispatch(verb, args, turn_id=turn_id)
        return turn.text

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, InterruptionFrame):
            self.cancel_current_turn()
            await self.push_frame(frame, direction)
            return

        if not isinstance(frame, LLMContextFrame):
            await self.push_frame(frame, direction)
            return

        self._current_turn_id = self._new_turn_id()
        await self.push_frame(LLMFullResponseStartFrame())
        await self.start_processing_metrics()
        try:
            text = await self.run_scripted_turn(turn_id=self._current_turn_id)
            await self.push_frame(LLMTextFrame(text))
        except Exception as exc:  # noqa: BLE001 — surface, never swallow, in the pipeline
            await self.push_error(error_msg=f"stub LLM turn failed: {exc}", exception=exc)
        finally:
            await self.stop_processing_metrics()
            await self.push_frame(LLMFullResponseEndFrame())
