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
from dataclasses import dataclass, field

from pipecat.services.llm_service import LLMService

from tv_avatar.control.bus import CommandBus


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


class StubLLMService(LLMService):
    """Emits fixed prose and fixed commands, ignoring its input entirely."""

    def __init__(
        self,
        bus: CommandBus,
        script: list[ScriptedTurn] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._bus = bus
        self._script = script or DEFAULT_SCRIPT
        self._index = 0

    def next_turn(self) -> ScriptedTurn:
        turn = self._script[self._index % len(self._script)]
        self._index += 1
        return turn

    async def run_scripted_turn(self, turn_id: str) -> str:
        """Dispatch this turn's commands, then return its prose.

        Commands go first so the UI reacts before speech completes, which
        is the ordering the real agent must also honour (spec §5).
        """
        turn = self.next_turn()
        for verb, args in turn.commands:
            await self._bus.dispatch(verb, args, turn_id=turn_id)
        return turn.text
