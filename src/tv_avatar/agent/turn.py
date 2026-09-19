"""Typed state of one agent turn.

A turn is one or more SGR cycles; each cycle streams one envelope and may
dispatch actions whose results feed the next cycle. These types are what the
loop passes around instead of positional parameters, a `marks` dict and
`(verb, dict)` tuples.
"""
import json
import time
from dataclasses import dataclass, field, fields
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


#: TV verbs whose title_id, emitted after recommendation results came back,
#: means the agent put that title in front of the viewer.
_OFFERING_VERBS = frozenset({"focus", "open_details", "play"})


@dataclass
class TurnTrace:
    """What the turn heard, said and pointed at — for memory ingest and the
    viewing log. Recommendation results are candidates; only the ones the agent
    then named or focused count as offered (see `offered_ids`)."""
    user_text: str = ""
    said: list[str] = field(default_factory=list)
    candidates: dict[str, str] = field(default_factory=dict)  # title_id -> name
    referenced_ids: set[str] = field(default_factory=set)

    def spoken(self) -> str:
        return "".join(self.said)

    def add_results(self, results: tuple["ToolResult", ...]) -> None:
        for r in results:
            if r.verb == "recommend_titles":
                for t in r.payload.get("titles") or []:
                    if t.get("title_id") and t.get("name"):
                        self.candidates[str(t["title_id"])] = str(t["name"])

    def add_action(self, verb: str, args: dict[str, Any], *, after_results: bool) -> None:
        if after_results and verb in _OFFERING_VERBS and args.get("title_id"):
            self.referenced_ids.add(str(args["title_id"]))

    def offered_ids(self, extra_spoken: str = "") -> list[str]:
        """Candidates the agent focused/opened/played, or named in what it said.
        Titles are spoken verbatim (persona rule), so a case-folded substring
        match on the name is the deliberate, simple heuristic."""
        spoken = (self.spoken() + " " + extra_spoken).casefold()
        return [tid for tid, name in self.candidates.items()
                if tid in self.referenced_ids or (name and name.casefold() in spoken)]


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
