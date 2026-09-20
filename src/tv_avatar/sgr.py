"""Schema-guided reasoning plumbing shared by every LLM call that decodes a plan.

A Pydantic model *is* the contract: its field order is the reasoning cascade
the model walks, and `response_format` hands the decoder a strict version of
its JSON schema. Both the turn envelope and the memory summary go through here,
so they cannot drift in how "strict" is spelled.
"""
from copy import deepcopy
from typing import Any

from pydantic import BaseModel

_DROP_KEYS = frozenset({"title", "default", "discriminator"})


def strictify(node: Any, *, keywords: bool = True) -> Any:
    """Make a pydantic schema acceptable to strict constrained decoders:
    every object closed and fully required, oneOf → anyOf, no defaults/titles.

    `keywords=False` marks a mapping whose keys are *field names*, not schema
    keywords: inside `properties`, a field called `title` is a property of the
    model, and dropping it would quietly shrink the decoding contract.
    """
    if isinstance(node, list):
        return [strictify(n) for n in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if keywords and key in _DROP_KEYS:
            continue
        if keywords and key == "oneOf":
            key = "anyOf"
        out[key] = strictify(value, keywords=not (keywords and key == "properties"))
    if out.get("type") == "object" and "properties" in out:
        out["additionalProperties"] = False
        out["required"] = list(out["properties"])
    return out


def response_format(model: type[BaseModel], name: str) -> dict:
    """The `json_schema` block of an OpenAI-style `response_format`."""
    return {"name": name, "strict": True, "schema": strictify(deepcopy(model.model_json_schema()))}
