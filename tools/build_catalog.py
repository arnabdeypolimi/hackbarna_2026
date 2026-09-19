"""Offline catalog indexing: TMDB CSV → data/catalog.parquet + data/qdrant_db.

Run once per dataset version. Resumable — an interrupted run skips titles
already in the index; an index built with another vector width is rebuilt.
The embedding provider must match the one the app queries with
(EMBEDDING_PROVIDER, default `local` = multilingual-e5-small on this machine,
~440 titles/s; `nebius` = Qwen3-Embedding-8B over the API). Reads
NEBIUS_API_KEY / NEBIUS_BASE_URL (+ EMBEDDING_*) from the environment or
.env; never prints them.

    uv run python tools/build_catalog.py --limit 1000   # smoke slice first
    uv run python tools/build_catalog.py                # full run
"""
import argparse
import sys
import time
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from loguru import logger
from openai import OpenAI
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from tv_avatar import e5
from tv_avatar.logging import setup_logging
from tv_avatar.recs.catalog import build_catalog

#: Whole rows (~700 chars) through E5 on CPU: bigger batches stop helping here.
LOCAL_BATCH_SIZE = 64


class BuildSettings(BaseSettings):
    """Subset of tv_avatar.config.Settings — an offline job needs no SLNG/Anam keys."""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    nebius_api_key: str = Field(default="", validation_alias=AliasChoices("NEBIUS_API_KEY", "OPENAI_API_KEY"))
    nebius_base_url: str = Field(default="https://api.tokenfactory.nebius.com/v1/",
                                 validation_alias=AliasChoices("NEBIUS_BASE_URL", "OPENAI_BASE_URL"))
    embedding_provider: Literal["local", "nebius"] = "local"
    embedding_model: str = "Qwen/Qwen3-Embedding-8B"
    embedding_dimensions: int = 1024
    catalog_path: str = "data/catalog.parquet"
    qdrant_path: str = "data/qdrant_db"
    catalog_index_limit: int = 100_000
    log_level: str = "INFO"


def nebius_embed_fn(settings: BuildSettings):
    if not settings.nebius_api_key:
        raise SystemExit("EMBEDDING_PROVIDER=nebius needs NEBIUS_API_KEY")
    client = OpenAI(api_key=settings.nebius_api_key, base_url=settings.nebius_base_url)

    def embed(texts: list[str]) -> list[list[float]]:
        for attempt in range(4):
            try:
                resp = client.embeddings.create(
                    model=settings.embedding_model, input=texts,
                    dimensions=settings.embedding_dimensions,
                )
                return [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]
            except Exception as err:  # noqa: BLE001 — shared serverless endpoint; retry with backoff
                wait = 2 ** attempt
                logger.warning("embedding batch failed ({}); retrying in {}s", type(err).__name__, wait)
                time.sleep(wait)
        raise RuntimeError("embedding endpoint failed 4 times in a row")

    return embed


def local_embed_fn(texts: list[str]) -> list[list[float]]:
    return e5.encode(texts, kind="passage")  # queries are embedded as "query:" at run time


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", default="data/TMDB_movie_dataset_v11.csv")
    parser.add_argument("--limit", type=int, default=None, help="override CATALOG_INDEX_LIMIT")
    parser.add_argument("--batch-size", type=int, default=None,
                        help=f"default {LOCAL_BATCH_SIZE} local, 256 nebius")
    args = parser.parse_args()

    settings = BuildSettings()
    setup_logging(settings.log_level)
    if settings.embedding_provider == "local":
        embed, dims, batch = local_embed_fn, e5.E5_DIM, args.batch_size or LOCAL_BATCH_SIZE
        logger.info("embedding locally with multilingual-e5-small ({} dims)", dims)
    else:
        embed, dims, batch = nebius_embed_fn(settings), settings.embedding_dimensions, args.batch_size or 256
        logger.info("embedding on Nebius with {} ({} dims)", settings.embedding_model, dims)

    t0 = time.perf_counter()
    report = build_catalog(
        args.csv, settings.catalog_path, settings.qdrant_path,
        limit=args.limit if args.limit is not None else settings.catalog_index_limit,
        embed_fn=embed, batch_size=batch, dims=dims,
    )
    logger.info(
        "catalog build done", rows=report.rows, embedded=report.embedded,
        skipped=report.skipped, seconds=round(time.perf_counter() - t0, 1),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
