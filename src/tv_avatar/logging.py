"""Loguru is the only logger (phase-2 global constraint).

Third-party libraries (VoiceMem, mem0, qdrant-client, openai, httpx) log
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
    for noisy in ("httpx", "httpx2", "httpcore", "openai._base_client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
