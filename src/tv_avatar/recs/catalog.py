"""Catalog store: parquet rows in memory + an embedded Qdrant vector index.

`title_id` is `str(tmdb_id)` end to end (D11). The Qdrant point id is the
same number as an int, so vectors can be fetched back by title without a
lookup table.
"""
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import polars as pl
from loguru import logger
from pydantic import BaseModel, Field
from qdrant_client import QdrantClient, models

COLLECTION = "tmdb"
EmbedFn = Callable[[list[str]], list[list[float]]]


class CatalogItem(BaseModel):
    title_id: str
    name: str
    year: int | None = None
    genres: list[str] = Field(default_factory=list)
    keywords: str = ""
    overview: str = ""
    vote_average: float = 0.0
    vote_count: int = 0
    popularity: float = 0.0
    poster_path: str | None = None
    imdb_id: str | None = None
    runtime: int | None = None
    original_language: str | None = None

    def embed_text(self) -> str:
        """The string that gets embedded — keep stable, the index depends on it."""
        return (
            f"{self.name} ({self.year}). Genres: {', '.join(self.genres)}. "
            f"Keywords: {self.keywords}. {self.overview[:600]}"
        )

    def label(self) -> str:
        """`Sicario (2015) — Crime, Thriller` for prompts and logs."""
        year = f" ({self.year})" if self.year else ""
        genres = f" — {', '.join(self.genres[:3])}" if self.genres else ""
        return f"{self.name}{year}{genres}"


class CatalogFilter(BaseModel):
    genres_any: set[str] = Field(default_factory=set)
    genres_none: set[str] = Field(default_factory=set)
    exclude_ids: set[str] = Field(default_factory=set)
    year_min: int | None = None
    year_max: int | None = None
    languages: set[str] = Field(default_factory=set)
    min_vote_count: int | None = None

    def to_qdrant(self) -> models.Filter | None:
        must: list[models.Condition] = []
        must_not: list[models.Condition] = []
        if self.genres_any:
            must.append(models.FieldCondition(key="genres", match=models.MatchAny(any=sorted(self.genres_any))))
        if self.languages:
            must.append(models.FieldCondition(key="original_language", match=models.MatchAny(any=sorted(self.languages))))
        if self.year_min is not None or self.year_max is not None:
            must.append(models.FieldCondition(key="year", range=models.Range(gte=self.year_min, lte=self.year_max)))
        if self.min_vote_count is not None:
            must.append(models.FieldCondition(key="vote_count", range=models.Range(gte=self.min_vote_count)))
        if self.genres_none:
            must_not.append(models.FieldCondition(key="genres", match=models.MatchAny(any=sorted(self.genres_none))))
        if self.exclude_ids:
            must_not.append(models.HasIdCondition(has_id=[int(i) for i in self.exclude_ids if i.isdigit()]))
        if not must and not must_not:
            return None
        return models.Filter(must=must or None, must_not=must_not or None)


# --- parquet schema ------------------------------------------------------

_SOURCE_COLUMNS = [
    "id", "title", "vote_average", "vote_count", "status", "release_date", "runtime",
    "imdb_id", "original_language", "overview", "popularity", "poster_path", "genres", "keywords",
]


def _normalise(lf: pl.LazyFrame) -> pl.LazyFrame:
    return lf.select(
        pl.col("id").cast(pl.Int64).cast(pl.Utf8).alias("title_id"),
        pl.col("title").alias("name"),
        pl.col("release_date").str.slice(0, 4).cast(pl.Int32, strict=False).alias("year"),
        pl.col("genres").fill_null("").str.split(", ").list.eval(pl.element().filter(pl.element() != "")).alias("genres"),
        pl.col("keywords").fill_null("").alias("keywords"),
        pl.col("overview").fill_null("").alias("overview"),
        pl.col("vote_average").cast(pl.Float64).fill_null(0.0),
        pl.col("vote_count").cast(pl.Int64).fill_null(0),
        pl.col("popularity").cast(pl.Float64).fill_null(0.0),
        pl.col("poster_path"),
        pl.col("imdb_id"),
        pl.col("runtime").cast(pl.Int32, strict=False),
        pl.col("original_language"),
    )


def _row_to_item(row: dict) -> CatalogItem:
    return CatalogItem.model_validate(row)


# --- store ---------------------------------------------------------------

class CatalogStore:
    def __init__(self, catalog_path: str | Path, qdrant_path: str | Path | None = None,
                 *, client: QdrantClient | None = None) -> None:
        if client is None and qdrant_path is None:
            raise ValueError("CatalogStore needs a qdrant_path or an injected client")
        self._client = client or QdrantClient(path=str(qdrant_path))
        frame = pl.read_parquet(catalog_path)
        self._items: dict[str, CatalogItem] = {
            row["title_id"]: _row_to_item(row) for row in frame.to_dicts()
        }
        self._popular: list[CatalogItem] = sorted(
            self._items.values(), key=lambda i: -i.popularity
        )
        logger.info("catalog loaded", titles=len(self._items), path=str(catalog_path))

    def __len__(self) -> int:
        return len(self._items)

    @property
    def client(self) -> QdrantClient:
        return self._client

    def lookup(self, title_id: str) -> CatalogItem | None:
        return self._items.get(str(title_id))

    def lookup_many(self, ids: Iterable[str]) -> list[CatalogItem]:
        return [item for i in ids if (item := self._items.get(str(i))) is not None]

    def top_popular(self, limit: int = 10, exclude: set[str] | None = None) -> list[CatalogItem]:
        skip = exclude or set()
        return [i for i in self._popular if i.title_id not in skip][:limit]

    def sample(self, limit: int = 8) -> list[CatalogItem]:
        return self.top_popular(limit)

    def search(self, vector: list[float], filters: CatalogFilter | None = None,
               limit: int = 20) -> list[tuple[CatalogItem, float]]:
        response = self._client.query_points(
            COLLECTION, query=vector, limit=limit,
            query_filter=filters.to_qdrant() if filters else None, with_payload=False,
        )
        out = []
        for point in response.points:
            item = self._items.get(str(point.id))
            if item is not None:
                out.append((item, float(point.score)))
        return out

    def vectors(self, ids: Iterable[str]) -> dict[str, list[float]]:
        numeric = [int(i) for i in ids if str(i).isdigit()]
        if not numeric:
            return {}
        records = self._client.retrieve(COLLECTION, ids=numeric, with_vectors=True, with_payload=False)
        return {str(r.id): list(r.vector) for r in records if r.vector is not None}


# --- offline build -------------------------------------------------------

@dataclass(frozen=True)
class BuildReport:
    rows: int
    embedded: int
    skipped: int


def load_source(csv_path: str | Path, limit: int | None) -> pl.DataFrame:
    lf = (
        pl.scan_csv(csv_path, infer_schema_length=10_000)
        .select(_SOURCE_COLUMNS)
        .filter(
            (pl.col("status") == "Released")
            & (pl.col("vote_count") >= 50)
            & pl.col("overview").is_not_null()
            & pl.col("genres").is_not_null()
            & pl.col("poster_path").is_not_null()
        )
        .sort("popularity", descending=True)
    )
    if limit is not None:
        lf = lf.head(limit)
    return _normalise(lf).collect()


def ensure_collection(client: QdrantClient, dims: int) -> None:
    if client.collection_exists(COLLECTION):
        return
    # Payload indexes are a no-op in embedded (local) Qdrant; filters still work.
    client.create_collection(
        COLLECTION, vectors_config=models.VectorParams(size=dims, distance=models.Distance.COSINE)
    )


def _existing_ids(client: QdrantClient, ids: list[int]) -> set[int]:
    if not client.collection_exists(COLLECTION):
        return set()
    found: set[int] = set()
    for i in range(0, len(ids), 1000):
        chunk = ids[i:i + 1000]
        found.update(int(r.id) for r in client.retrieve(COLLECTION, ids=chunk, with_payload=False))
    return found


def build_catalog(csv_path: str | Path, out_parquet: str | Path, qdrant_path: str | Path | None = None,
                  *, limit: int | None = None, embed_fn: EmbedFn, client: QdrantClient | None = None,
                  batch_size: int = 256, dims: int | None = None) -> BuildReport:
    """CSV → parquet + Qdrant. Resumable: titles already indexed are skipped."""
    if client is None and qdrant_path is None:
        raise ValueError("build_catalog needs a qdrant_path or an injected client")
    client = client or QdrantClient(path=str(qdrant_path))
    frame = load_source(csv_path, limit)
    Path(out_parquet).parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(out_parquet)
    items = [_row_to_item(r) for r in frame.to_dicts()]
    logger.info("catalog parquet written", rows=len(items), path=str(out_parquet))

    existing = _existing_ids(client, [int(i.title_id) for i in items])
    todo = [i for i in items if int(i.title_id) not in existing]
    embedded = 0
    for start in range(0, len(todo), batch_size):
        batch = todo[start:start + batch_size]
        vectors = embed_fn([i.embed_text() for i in batch])
        if dims is None:
            dims = len(vectors[0])
        ensure_collection(client, dims)
        client.upsert(COLLECTION, points=[
            models.PointStruct(
                id=int(item.title_id), vector=vec,
                payload={"title_id": item.title_id, "name": item.name, "genres": item.genres,
                         "year": item.year, "original_language": item.original_language,
                         "vote_average": item.vote_average, "vote_count": item.vote_count,
                         "popularity": item.popularity},
            )
            for item, vec in zip(batch, vectors, strict=True)
        ])
        embedded += len(batch)
        logger.info("catalog batch indexed", done=embedded, total=len(todo))
    return BuildReport(rows=len(items), embedded=embedded, skipped=len(existing))
