"""Embedding clients. The catalog and every query must share one vector space,
so the query embedder is the same model the offline build used."""
import asyncio
import hashlib
import math
from typing import Protocol

from openai import AsyncOpenAI


class Embedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIEmbedder:
    def __init__(self, base_url: str, api_key: str, model: str, dimensions: int,
                 client: AsyncOpenAI | None = None) -> None:
        self._client = client or AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._model = model
        self._dimensions = dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        resp = await self._client.embeddings.create(
            model=self._model, input=texts, dimensions=self._dimensions
        )
        return [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]


class FakeEmbedder:
    """Deterministic token-hash vectors; optional artificial latency for timeout tests."""

    def __init__(self, dims: int = 8, delay_s: float = 0.0) -> None:
        self.dims = dims
        self.delay_s = delay_s
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        return [hash_embed(t, self.dims) for t in texts]


def hash_embed(text: str, dims: int) -> list[float]:
    vec = [0.0] * dims
    for tok in text.lower().split():
        vec[int(hashlib.md5(tok.encode()).hexdigest(), 16) % dims] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]
