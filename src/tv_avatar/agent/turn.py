"""Typed state of one agent turn.

A turn is one or more SGR cycles; each cycle streams one envelope and may
dispatch actions whose *observations* feed the next cycle. These types are what
the loop passes around instead of positional parameters, a `marks` dict and
`(verb, dict)` tuples.
"""
import json
import time
from dataclasses import dataclass, field, fields
from typing import Any, ClassVar, Literal

from pydantic import ValidationError

from tv_avatar import tracing as tel
from tv_avatar.agent.envelope import REGISTRY, FinalTurnPlan, Request, TurnPlan
from tv_avatar.tracing import (
    ATTR_TURN_PREFIX,
    META_CYCLES,
    META_FALLBACK,
    META_INTENT,
    META_OPERATION,
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
class CycleTelemetry:
    stop_reason: Literal["provider_eof", "envelope_complete", "budget_exceeded",
                         "interrupted", "provider_error"] = "provider_eof"
    finish_reason: str | None = None
    usage_available: bool = False
    envelope_complete: bool = False
    validation: str = "incomplete"
    first_content_ms: int | None = None
    first_sentence_ms: int | None = None
    generation_ms: int | None = None
    total_ms: int | None = None
    accepted: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    sentence_index: int = 0

    def plan(self, raw: str, *, final: bool) -> dict:
        plan = None
        errors = []
        try:
            parsed = json.loads(raw)
        except (ValueError, RecursionError):
            self.validation = "invalid" if self.envelope_complete else "incomplete"
        else:
            try:
                plan = (FinalTurnPlan if final else TurnPlan).model_validate(parsed).model_dump()
                self.validation = "valid"
            except ValidationError as err:
                self.validation = "invalid"
                errors = [{"path": list(e["loc"]), "code": e["type"]}
                          for e in err.errors(include_input=False, include_context=False)]
        return {"plan": plan, "validation": self.validation, "errors": errors,
                "accepted_actions": self.accepted, "rejected_actions": self.rejected}

    def as_log_fields(self) -> dict:
        return {name: getattr(self, name) for name in (
            "stop_reason", "finish_reason", "usage_available", "validation", "first_content_ms",
            "first_sentence_ms", "generation_ms", "total_ms") if getattr(self, name) is not None}

    def as_span_attributes(self) -> dict:
        mapping = {
            "stop_reason": tel.META_STOP_REASON, "finish_reason": tel.META_FINISH_REASON,
            "usage_available": tel.META_USAGE_AVAILABLE, "validation": tel.META_VALIDATION,
            "first_content_ms": tel.ATTR_FIRST_CONTENT_MS,
            "first_sentence_ms": tel.ATTR_FIRST_SENTENCE_MS,
            "generation_ms": tel.ATTR_GENERATION_MS, "total_ms": tel.ATTR_CYCLE_TOTAL_MS,
        }
        return {mapping[k]: v for k, v in self.as_log_fields().items()}


@dataclass
class TurnMetrics:
    """What one `turn` INFO line reports. `None` means "did not happen"."""
    cycles: int = 0
    n_actions: int = 0
    intent: str | None = None
    operation: str | None = None
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
        "intent": META_INTENT, "operation": META_OPERATION, "cycles": META_CYCLES,
        "fallback": META_FALLBACK}

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
    rail_ids: list[str] = field(default_factory=list)
    submitted: list[str] = field(default_factory=list)

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
        if after_results and verb == "show_titles":
            self.rail_ids = list(dict.fromkeys(str(tid) for tid in args.get("title_ids", [])))

    def offered_ids(self) -> list[str]:
        """Candidates the agent focused/opened/played, or named in what it said
        (the templated fallback counts as saying). Titles are spoken verbatim
        (persona rule), so a case-folded substring match on the name is the
        deliberate, simple heuristic.

        Pointed-at ids come first: `rec_shown` events of one turn share a
        timestamp and the greeting reopens with the first one rendered, so the
        title the agent actually focused must lead, not the tool's first hit."""
        spoken = (self.spoken() + " " + self.fallback_said).casefold()
        shown = [tid for tid in self.rail_ids if tid in self.candidates]
        pointed = [tid for tid in self.candidates if tid in self.referenced_ids and tid not in shown]
        named = [tid for tid, name in self.candidates.items()
                 if tid not in self.referenced_ids and tid not in shown and name and name.casefold() in spoken]
        return shown + pointed + named


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
        """Every awaited reply reaches the model — a result, an error, or a
        successful TV search — so the viewer hears what came back rather than
        only the filler. Cancelled replies belong to an interrupted turn and
        must not start another cycle."""
        spec = REGISTRY.get(self.verb)
        return spec is not None and spec.awaits_result and self.payload.get("status") != "cancelled"


@dataclass(frozen=True)
class CycleOutcome:
    """What one cycle left for the next: the raw envelope and the observations
    from awaited tools, including successful TV search replies."""
    raw: str
    observations: tuple[ToolResult, ...]
    #: What the viewer asked for, as this cycle decoded it; the next cycle is
    #: pinned to its operation.
    request: Request | None = None

    @property
    def done(self) -> bool:
        """Nothing for the model to see: its `say` was the answer and the turn ends."""
        return not self.observations

    def feedback(self) -> str:
        return json.dumps({r.verb: r.payload for r in self.observations}, ensure_ascii=False)
