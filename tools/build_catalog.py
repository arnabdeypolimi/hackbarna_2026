"""Offline catalog indexing: TMDB CSV → data/catalog.parquet + data/qdrant_db.

Run once per dataset version. Resumable — an interrupted run skips titles
already in the index. Reads OPENAI_API_KEY / OPENAI_BASE_URL (+ EMBEDDING_*)
from the environment or .env; never prints them.

    uv run python tools/build_catalog.py --limit 1000   # smoke slice first
    uv run python tools/build_catalog.py                # full run
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from loguru import logger  # noqa: E402
from openai import OpenAI  # noqa: E402
from pydantic_settings import BaseSettings, SettingsConfigDict  # noqa: E402

from tv_avatar.logging import setup_logging  # noqa: E402
from tv_avatar.recs.catalog import build_catalog  # noqa: E402


class BuildSettings(BaseSettings):
    """Subset of tv_avatar.config.Settings — an offline job needs no SLNG/Anam keys."""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openai_api_key: str
    openai_base_url: str = "https://api.tokenfactory.nebius.com/v1/"
    embedding_model: str = "Qwen/Qwen3-Embedding-8B"
    embedding_dimensions: int = 1024
    catalog_path: str = "data/catalog.parquet"
    qdrant_path: str = "data/qdrant_db"
    catalog_index_limit: int = 100_000
    log_level: str = "INFO"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", default="data/TMDB_movie_dataset_v11.csv")
    parser.add_argument("--limit", type=int, default=None, help="override CATALOG_INDEX_LIMIT")
    parser.add_argument("--batch-size", type=int, default=256)
    args = parser.parse_args()

    settings = BuildSettings()
    setup_logging(settings.log_level)
    client = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)

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

    t0 = time.perf_counter()
    report = build_catalog(
        args.csv, settings.catalog_path, settings.qdrant_path,
        limit=args.limit if args.limit is not None else settings.catalog_index_limit,
        embed_fn=embed, batch_size=args.batch_size, dims=settings.embedding_dimensions,
    )
    logger.info(
        "catalog build done", rows=report.rows, embedded=report.embedded,
        skipped=report.skipped, seconds=round(time.perf_counter() - t0, 1),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
