"""Typed state of one agent turn.

A turn is one or more SGR cycles; each cycle streams one envelope and may
dispatch actions whose *observations* feed the next cycle. These types are what
the loop passes around instead of positional parameters, a `marks` dict and
`(verb, dict)` tuples.
"""
import json
import time
from dataclasses import dataclass, field, fields
from typing import Any, ClassVar

from tv_avatar.agent.envelope import REGISTRY
from tv_avatar.tracing import (
    ATTR_TURN_PREFIX,
    META_CYCLES,
    META_FALLBACK,
    META_INTENT,
)


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

    def mark_once(self, name: str, value: int | str) -> None:
        """First-seen value (TTFT, first action, intent): later cycles do not
        overwrite it. Cycle 1 is the routing decision; the answer cycle after
        tool results usually says `answer`, which is not what the turn was."""
        if getattr(self, name) is None:
            setattr(self, name, value)

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

    #: The same fields, split for Langfuse (D16, D20): what you filter turns by
    #: goes under observation metadata, the timings are `tv.turn.*` details.
    _FILTERABLE: ClassVar[dict[str, str]] = {
        "intent": META_INTENT, "cycles": META_CYCLES, "fallback": META_FALLBACK}

    def as_span_attributes(self) -> dict[str, int | str | bool]:
        """`None` is dropped (OTel rejects it); `fallback` only when true."""
        out: dict[str, int | str | bool] = {}
        for name, value in self.as_log_fields().items():
            if value is not None:
                out[self._FILTERABLE.get(name, ATTR_TURN_PREFIX + name)] = value
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
    #: The templated answer, when a cycle blew its budget. Deliberately not in
    #: `said`: a template built from substitute results is not the agent's reply,
    #: and memory must not learn the viewer "wanted" whatever came back.
    fallback_said: str = ""
    candidates: dict[str, str] = field(default_factory=dict)  # title_id -> name
    referenced_ids: set[str] = field(default_factory=set)

    def spoken(self) -> str:
        return "".join(self.said).strip()

    def begin_cycle(self) -> None:
        """The memory transcript reads "Let me look. I found…", not "look.I found"."""
        if self.said:
            self.said.append(" ")

    def add_results(self, results: tuple["ToolResult", ...]) -> None:
        for r in results:
            if r.verb == "recommend_titles":
                for t in r.payload.get("titles") or []:
                    if t.get("title_id") and t.get("name"):
                        self.candidates[str(t["title_id"])] = str(t["name"])

    def add_action(self, verb: str, args: dict[str, Any], *, after_results: bool) -> None:
        if after_results and verb in _OFFERING_VERBS and args.get("title_id"):
            self.referenced_ids.add(str(args["title_id"]))

    def offered_ids(self) -> list[str]:
        """Candidates the agent focused/opened/played, or named in what it said
        (the templated fallback counts as saying). Titles are spoken verbatim
        (persona rule), so a case-folded substring match on the name is the
        deliberate, simple heuristic.

        Pointed-at ids come first: `rec_shown` events of one turn share a
        timestamp and the greeting reopens with the first one rendered, so the
        title the agent actually focused must lead, not the tool's first hit."""
        spoken = (self.spoken() + " " + self.fallback_said).casefold()
        pointed = [tid for tid in self.candidates if tid in self.referenced_ids]
        named = [tid for tid, name in self.candidates.items()
                 if tid not in self.referenced_ids and name and name.casefold() in spoken]
        return pointed + named


#: Reply statuses that mean the awaited action did not happen (bus timeout,
#: internal tool error, rejected args). "cancelled" is deliberately absent: the
#: turn is already being torn down by a barge-in and must not speak again.
FAILED_STATUSES = frozenset({"unavailable", "error", "invalid"})


@dataclass(frozen=True)
class ToolResult:
    """One awaited action's reply, as the model will see it."""
    verb: str
    payload: dict[str, Any]

    @property
    def failed(self) -> bool:
        return self.payload.get("status") in FAILED_STATUSES

    @property
    def is_observation(self) -> bool:
        """A reply the model must see. Observation-returning tools always; an
        awaited TV verb only when it failed — a successful `search_catalog` is
        answered by the TV screen, but with no TV to answer (seen live,
        2026-09-20) the filler "Searching for X." was the whole turn and the
        viewer waited on nothing. The failure is fed back so the agent says so."""
        spec = REGISTRY.get(self.verb)
        if spec is None:
            return False
        return spec.returns_observation or (spec.awaits_result and self.failed)


@dataclass(frozen=True)
class CycleOutcome:
    """What one cycle left for the next: the raw envelope and the observations
    (only those — a successful TV reply is the screen's business, not the model's)."""
    raw: str
    observations: tuple[ToolResult, ...]

    @property
    def done(self) -> bool:
        """Nothing for the model to see: its `say` was the answer and the turn ends."""
        return not self.observations

    def feedback(self) -> str:
        return json.dumps({r.verb: r.payload for r in self.observations}, ensure_ascii=False)
