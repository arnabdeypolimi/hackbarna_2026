"""Loguru is the only logger (phase-2 global constraint).

Third-party libraries (qdrant-client, sentence-transformers, openai, httpx) log
through stdlib ``logging``; a single intercept handler on the root logger
turns those records into loguru records so one sink sees everything and
``session_id``/``turn_id`` bindings stay the sole way to correlate a turn.
"""
import inspect
import logging
import sys

from loguru import logger

_FORMAT = (
    "<green>{time:HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | "
    "{extra[ctx]}<cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>{extra[kv]}"
)
_CTX_KEYS = ("session_id", "user_id", "turn_id")
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
    parts = [f"{k}={extra[k]}" for k in _CTX_KEYS if k in extra]
    extra["ctx"] = f"[{' '.join(parts)}] " if parts else ""
    kv = [f"{k}={v}" for k, v in extra.items() if k not in _CTX_KEYS and k not in _INTERNAL_KEYS]
    extra["kv"] = f" | {' '.join(kv)}" if kv else ""


def setup_logging(level: str = "INFO") -> None:
    """Idempotent: safe to call from ``create_app`` and from CLI tools."""
    logger.remove()
    logger.configure(patcher=_add_ctx)
    logger.add(sys.stderr, level=level.upper(), format=_FORMAT, enqueue=False)
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
        logger.remove()
        logger.add(sys.stderr, level="DEBUG", format=_FORMAT, enqueue=False, filter=_quiet_pipecat)


def _quiet_pipecat(record: dict) -> bool:
    name = record["name"] or ""
    if record["level"].no > logger.level("DEBUG").no:
        return True
    return not name.startswith(("pipecat.", "pipecat_slng", "pipecat_anam", "anam"))
