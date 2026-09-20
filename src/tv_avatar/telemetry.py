import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tv_avatar.config import Settings

_SECRET_KEY = re.compile(
    r"authorization|password|secret|api[_-]?key|(?:^|[._-])token$|control[_-]?token|headers",
    re.IGNORECASE)
_SECRET_TEXT = re.compile(r"(?i)(bearer\s+|basic\s+|[?&](?:token|api_key)=)[^\s&\"']+")


@dataclass(frozen=True)
class ContentPolicy:
    content: bool = True
    max_chars: int = 16384
    secrets: tuple[str, ...] = field(default=(), repr=False)

    @classmethod
    def from_settings(cls, settings: "Settings") -> "ContentPolicy":
        secrets = tuple(value for value in (
            settings.nebius_api_key, settings.slng_api_key, settings.anam_api_key,
            settings.langfuse_public_key, settings.langfuse_secret_key,
        ) if len(value) >= 8)
        return cls(settings.trace_content, settings.telemetry_max_chars, secrets)

    def text(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        value = _SECRET_TEXT.sub(lambda m: m[1] + "[REDACTED]", value)
        return value

    def clean(self, value: Any, *, depth: int = 0) -> Any:
        if depth > 12:
            return "[TRUNCATED]"
        if isinstance(value, dict):
            result = {str(k): "[REDACTED]" if _SECRET_KEY.search(str(k)) else
                      self.clean(v, depth=depth + 1) for k, v in list(value.items())[:128]}
            if len(value) > 128:
                result["_telemetry_truncated"] = True
            return result
        if isinstance(value, (list, tuple)):
            result = [self.clean(v, depth=depth + 1) for v in value[:128]]
            return result + (["[TRUNCATED]"] if len(value) > 128 else [])
        if isinstance(value, str):
            return self.text(value)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return type(value).__name__

    def encode(self, value: Any) -> str | None:
        if not self.content:
            return None
        encoded = json.dumps(self.clean(value), ensure_ascii=False)
        if len(encoded) > self.max_chars:
            return json.dumps({"truncated": True, "original_chars": len(encoded),
                               "preview": encoded[:self.max_chars]}, ensure_ascii=False)
        return encoded

    def attribute(self, value: Any) -> Any:
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except (ValueError, RecursionError):
                parsed = None
            if isinstance(parsed, (dict, list)):
                cleaned = self.clean(parsed)
                if cleaned != parsed:
                    value = json.dumps(cleaned, ensure_ascii=False)
            else:
                value = self.text(value)
            if len(value) > self.max_chars:
                return json.dumps({"truncated": True, "original_chars": len(value),
                                   "preview": value[:self.max_chars]}, ensure_ascii=False)
            return value
        if isinstance(value, (list, tuple)):
            return tuple(self.attribute(v) for v in value[:128])
        return value

    def attributes(self, values: Any, *, content_keys: frozenset[str]) -> dict:
        return {k: self.attribute(v) for k, v in (values or {}).items()
                if not _SECRET_KEY.search(k) and (self.content or k not in content_keys)}
