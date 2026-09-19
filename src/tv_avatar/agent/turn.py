"""Typed state of one agent turn.

A turn is one or more SGR cycles; each cycle streams one envelope and may
dispatch actions whose results feed the next cycle. These types are what the
loop passes around instead of positional parameters, a `marks` dict and
`(verb, dict)` tuples.
"""
import json
import time
from dataclasses import dataclass, fields
from typing import Any

from tv_avatar.agent.envelope import REGISTRY


@dataclass(frozen=True)
class TurnContext:
    """Identity of the turn, fixed at open — threaded through every helper."""
    turn_id: str
    user_id: str
    t0: float
    log: Any  # loguru logger bound with session_id / user_id / turn_id

    def elapsed_ms(self) -> int:
        return round((time.perf_counter() - self.t0) * 1000)


@dataclass
class TurnMetrics:
    """What one `turn` INFO line reports. `None` means "did not happen"."""
    cycles: int = 0
    n_actions: int = 0
    intent: str | None = None
    recall_ms: int | None = None
    ttft_ms: int | None = None
    first_action_ms: int | None = None
    total_ms: int | None = None
    fallback: bool = False

    def mark_once(self, name: str, ms: int) -> None:
        """First-seen timing (TTFT, first action): later cycles do not overwrite it."""
        if getattr(self, name) is None:
            setattr(self, name, ms)

    def as_log_fields(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name == "fallback":
                if value:
                    out[f.name] = True
            elif value is not None or f.name in ("cycles", "n_actions", "intent"):
                out[f.name] = value
        return out


@dataclass(frozen=True)
class ToolResult:
    """One awaited action's reply, as the model will see it."""
    verb: str
    payload: dict[str, Any]

    @property
    def earns_cycle(self) -> bool:
        spec = REGISTRY.get(self.verb)
        return spec is not None and spec.earns_cycle


@dataclass(frozen=True)
class CycleOutcome:
    raw: str
    results: tuple[ToolResult, ...]

    @property
    def needs_another_cycle(self) -> bool:
        """Any cycle-earning tool result — including a failed one, so the agent
        speaks the fallback instead of stopping at the filler."""
        return any(r.earns_cycle for r in self.results)

    def feedback(self) -> str:
        return json.dumps({r.verb: r.payload for r in self.results}, ensure_ascii=False)
