"""Incremental extractor for the streamed turn envelope.

Feeds raw JSON text a chunk at a time — chunk boundaries are arbitrary — and
emits `SayDelta` for characters inside the top-level `"say"` string as they
arrive, `ActionReady` for each completed element of `"actions"`, `IntentReady`
once the intent string closes, and `Done` when the top-level object closes.
Handles any key order. Never raises: malformed input yields nothing more.
"""
import json
from dataclasses import dataclass

_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


@dataclass(frozen=True)
class SayDelta:
    text: str


@dataclass(frozen=True)
class ActionReady:
    action: dict


@dataclass(frozen=True)
class IntentReady:
    intent: str


@dataclass(frozen=True)
class Done:
    pass


Event = SayDelta | ActionReady | IntentReady | Done


class EnvelopeStreamer:
    def __init__(self) -> None:
        self._depth = 0
        self._stack: list[str] = []            # "obj" | "arr"
        self._expect_key = False
        self._in_str = False
        self._esc = False
        self._uni: str | None = None
        self._str_role = "value"
        self._str_buf: list[str] = []
        self._cur_key = ""
        self._top_key: str | None = None
        self._say_mode = False
        self._action_buf: list[str] | None = None
        self._done = False
        self._dead = False

    @property
    def finished(self) -> bool:
        return self._done

    def feed(self, delta: str) -> list[Event]:
        if self._done or self._dead or not delta:
            return []
        events: list[Event] = []
        say: list[str] = []
        try:
            for ch in delta:
                self._step(ch, events, say)
                if self._done:
                    break
        except Exception:  # noqa: BLE001 — never raise mid-stream; the turn reports failure at the end
            self._dead = True
        if say:
            events.insert(self._say_insert_index(events), SayDelta("".join(say)))
        return events

    # A SayDelta accumulated across a chunk belongs before any event that
    # happened after the say string closed in that same chunk.
    def _say_insert_index(self, events: list[Event]) -> int:
        for i, e in enumerate(events):
            if isinstance(e, (ActionReady, Done)) and not self._say_mode:
                return i
        return len(events)

    def _collect(self, ch: str) -> None:
        if self._action_buf is not None:
            self._action_buf.append(ch)

    def _emit_char(self, ch: str, say: list[str]) -> None:
        self._str_buf.append(ch)
        if self._say_mode:
            say.append(ch)

    def _step(self, ch: str, events: list[Event], say: list[str]) -> None:
        if self._in_str:
            self._collect(ch)
            if self._uni is not None:
                self._uni += ch
                if len(self._uni) == 4:
                    self._emit_char(chr(int(self._uni, 16)), say)
                    self._uni = None
                return
            if self._esc:
                self._esc = False
                if ch == "u":
                    self._uni = ""
                else:
                    self._emit_char(_ESCAPES.get(ch, ch), say)
                return
            if ch == "\\":
                self._esc = True
                return
            if ch == '"':
                self._in_str = False
                self._end_string(events)
                return
            self._emit_char(ch, say)
            return

        if ch == '"':
            self._in_str = True
            self._str_buf = []
            self._str_role = "key" if self._expect_key else "value"
            self._expect_key = False
            if self._str_role == "value" and self._depth == 1 and self._top_key == "say":
                self._say_mode = True
            self._collect(ch)
        elif ch == "{":
            self._depth += 1
            self._stack.append("obj")
            self._expect_key = True
            if self._depth == 3 and self._top_key == "actions" and self._action_buf is None:
                self._action_buf = ["{"]
            else:
                self._collect(ch)
        elif ch == "}":
            self._collect(ch)
            if self._action_buf is not None and self._depth == 3:
                self._flush_action(events)
            self._depth -= 1
            if self._stack:
                self._stack.pop()
            if self._depth == 0:
                self._done = True
                events.append(Done())
        elif ch == "[":
            self._depth += 1
            self._stack.append("arr")
            self._collect(ch)
        elif ch == "]":
            self._collect(ch)
            self._depth -= 1
            if self._stack:
                self._stack.pop()
            if self._depth == 1:
                self._top_key = None
        elif ch == ":":
            if self._depth == 1:
                self._top_key = self._cur_key
            self._collect(ch)
        elif ch == ",":
            if self._stack and self._stack[-1] == "obj":
                self._expect_key = True
            if self._depth == 1:
                self._top_key = None
            self._collect(ch)
        else:
            self._collect(ch)

    def _end_string(self, events: list[Event]) -> None:
        text = "".join(self._str_buf)
        if self._str_role == "key" and self._depth == 1:
            self._cur_key = text
        elif self._str_role == "value" and self._depth == 1:
            if self._say_mode:
                self._say_mode = False
            elif self._top_key == "intent":
                events.append(IntentReady(text))
            self._top_key = None

    def _flush_action(self, events: list[Event]) -> None:
        raw = "".join(self._action_buf or [])
        self._action_buf = None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return
        if isinstance(parsed, dict):
            events.append(ActionReady(parsed))
