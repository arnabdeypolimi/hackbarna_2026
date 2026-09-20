"""Loguru is the only logger (phase-2 global constraint).

Third-party libraries (qdrant-client, sentence-transformers, openai, httpx) log
through stdlib ``logging``; a single intercept handler on the root logger
turns those records into loguru records so one sink sees everything and
``session_id``/``turn_id`` bindings stay the sole way to correlate a turn.
"""
import contextlib
import inspect
import json
import logging
import sys
import traceback
from queue import Full, Queue
from threading import Event, Thread
from typing import TYPE_CHECKING

from loguru import logger
from opentelemetry import baggage, trace

from tv_avatar.telemetry import ContentPolicy

if TYPE_CHECKING:
    from tv_avatar.config import Settings

_policy = ContentPolicy()
_sink = None
_STOP = object()  # queued last by stop(): everything before it is still written
_SAFE_FIELDS = frozenset({
    "session_id", "user_id", "turn_id", "trace_id", "span_id", "event", "step",
    "cycle", "action_index", "intent", "verb", "status", "source", "stop_reason",
    "validation", "finish_reason", "usage_available", "schema_name", "interrupted", "operation",
    "cycles", "n_actions", "fallback", "recall_ms", "ttft_ms", "first_action_ms",
    "total_ms", "first_content_ms", "generation_ms", "ms", "budget_ms", "n",
    "dropped_commands", "error_type", "command_id", "reply_type", "sentence_index",
    "observations", "command_replies", "first_sentence_ms",
})


class BackgroundSink:
    def __init__(self, stream, *, json_output: bool, capacity: int = 2048) -> None:
        self.stream = stream
        self.json_output = json_output
        self.queue = Queue(maxsize=capacity)
        self.dropped = 0
        self.failures = 0
        self.closed = Event()
        self.worker = Thread(target=self._run, daemon=True, name="telemetry-logs")
        self.worker.start()

    def write(self, message) -> None:
        if self.closed.is_set():
            return
        record = message.record
        data = ({"timestamp": record["time"].isoformat(), "level": record["level"].name,
                 "event": record["extra"].get("event", record["message"]),
                 "message": record["message"],
                 **{k: v for k, v in record["extra"].items() if k not in _INTERNAL_KEYS},
                 **({"exception": _policy.text("".join(traceback.format_exception(
                     record["exception"].type, record["exception"].value, record["exception"].traceback)))}
                    if record["exception"] is not None else {})}
                if self.json_output else str(message))
        try:
            self.queue.put_nowait(data)
        except Full:
            self.dropped += 1

    def _run(self) -> None:
        reported = 0
        while True:
            data = self.queue.get()  # blocks; no polling wake-ups from idle sinks
            try:
                if data is _STOP:
                    return
                if isinstance(data, Event):
                    data.set()
                    continue
                if self.dropped > reported:
                    warning = {"level": "WARNING", "event": "telemetry.logs.dropped",
                               "count": self.dropped - reported}
                    self.stream.write(json.dumps(warning) + "\n")
                    reported = self.dropped
                self.stream.write(json.dumps(data, ensure_ascii=False, default=str) + "\n"
                                  if self.json_output else data)
                self.stream.flush()
            except (OSError, ValueError):
                self.failures += 1
            finally:
                self.queue.task_done()

    def flush(self, timeout: float = 2.0) -> bool:
        done = Event()
        try:
            self.queue.put(done, timeout=timeout)
        except Full:
            return False
        return done.wait(timeout)

    def stop(self) -> None:
        """Drain what is queued, then let the worker exit. Records written after
        this are dropped."""
        self.closed.set()
        with contextlib.suppress(Full):  # a stuck stream must not hang the caller too
            self.queue.put(_STOP, timeout=2.0)
        self.worker.join(timeout=2.0)


def flush_logging() -> bool:
    return _sink.flush() if _sink is not None else True

_FORMAT = (
    "<green>{time:HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | "
    "{extra[ctx]}<cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>{extra[kv]}"
)
# The agent's own lines — intent, envelope, actions, the per-turn summary — in
# dark purple so its reasoning stands out from pipeline and library chatter.
_AGENT_COLOR = "fg #8700af"
_AGENT_FORMAT = (
    "<green>{time:HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | "
    f"{{extra[ctx]}}<{_AGENT_COLOR}>{{name}}:{{function}} - {{message}}{{extra[kv]}}</{_AGENT_COLOR}>"
)
_AGENT_MODULES = ("tv_avatar.agent.",)
_CTX_KEYS = ("session_id", "user_id", "turn_id")


def _format(record: dict) -> str:
    # A callable format must supply the newline and exception slot itself.
    fmt = _AGENT_FORMAT if (record["name"] or "").startswith(_AGENT_MODULES) else _FORMAT
    return fmt + "\n{exception}"
_INTERNAL_KEYS = frozenset(("ctx", "kv"))


class InterceptHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        level: str | int
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = inspect.currentframe(), 0
        while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def _add_ctx(record: dict) -> None:
    extra = record["extra"]
    for key, attribute in (("session_id", "langfuse.session.id"), ("user_id", "langfuse.user.id"),
                           ("turn_id", "langfuse.observation.metadata.turn_id"),
                           ("cycle", "langfuse.observation.metadata.cycle"),
                           ("action_index", "langfuse.observation.metadata.action_index")):
        value = baggage.get_baggage(attribute)
        if value is not None:
            extra.setdefault(key, value)
    # The traceback itself stays: `{exception}` in the format prints it in text
    # mode and the JSON sink serialises it. Only TRACE_CONTENT=false strips it.
    if record["exception"] is not None:
        extra["error_type"] = record["exception"].type.__name__
    span_context = trace.get_current_span().get_span_context()
    if span_context.is_valid:
        extra.setdefault("trace_id", format(span_context.trace_id, "032x"))
        extra.setdefault("span_id", format(span_context.span_id, "016x"))
    if not _policy.content:
        record["message"] = extra.get("event", "log.record")
        record["exception"] = None
        for key in list(extra):
            if key not in _SAFE_FIELDS:
                del extra[key]
    record["message"] = _policy.attribute(record["message"])
    for key, value in list(extra.items()):
        extra[key] = _policy.clean({key: value})[key]
        if isinstance(extra[key], (str, list, tuple)):
            extra[key] = _policy.attribute(extra[key])
        elif isinstance(extra[key], dict):
            extra[key] = json.loads(_policy.encode(extra[key]) or "null")
    parts = [f"{k}={extra[k]}" for k in _CTX_KEYS if k in extra]
    extra["ctx"] = f"[{' '.join(parts)}] " if parts else ""
    kv = [f"{k}={v}" for k, v in extra.items() if k not in _CTX_KEYS and k not in _INTERNAL_KEYS]
    extra["kv"] = f" | {' '.join(kv)}" if kv else ""


def setup_logging(level: str = "INFO", *, settings: "Settings | None" = None,
                  json_output: bool = False, content: bool = True) -> None:
    """Idempotent: safe to call from ``create_app`` and from CLI tools."""
    global _policy, _sink
    logger.remove()
    _policy = ContentPolicy.from_settings(settings) if settings else ContentPolicy(content=content)
    json_output = settings.log_format == "json" if settings else json_output
    if _sink is not None:
        _sink.stop()  # otherwise every call leaks a worker thread
    _sink = BackgroundSink(sys.stderr, json_output=json_output)
    logger.configure(patcher=_add_ctx)
    logger.add(_sink, level=level.upper(), format=_format, enqueue=False,
               backtrace=False, diagnose=False,
               filter=_quiet_pipecat if level.upper() == "DEBUG" else None)
    root = logging.getLogger()
    if not any(isinstance(h, InterceptHandler) for h in root.handlers):
        root.handlers = [InterceptHandler()]
    root.setLevel(logging.DEBUG if level.upper() == "DEBUG" else logging.INFO)
    # Third-party DEBUG chatter that would bury the agent trace.
    for noisy in ("httpx", "httpx2", "httpcore", "openai._base_client", "urllib3", "huggingface_hub",
                  "filelock", "sentence_transformers", "aiortc", "aioice", "asyncio",
                  "websockets", "websockets.client", "websockets.protocol", "aiosqlite", "anam"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # Pipecat and the SLNG plugin log through loguru directly; their per-frame
    # DEBUG lines (audio chunks, metrics, worker bookkeeping) are filtered by
    # module so our own DEBUG trace stays readable. LOG_LEVEL=TRACE shows all.
    if level.upper() == "DEBUG":
        logging.getLogger().setLevel(logging.DEBUG)


def _quiet_pipecat(record: dict) -> bool:
    name = record["name"] or ""
    if record["level"].no > logger.level("DEBUG").no:
        return True
    return not name.startswith(("pipecat.", "pipecat_slng", "pipecat_anam", "anam"))
