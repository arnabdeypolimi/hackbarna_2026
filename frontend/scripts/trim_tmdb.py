"""Trim the TMDB movie dataset to a small CSV the TV app can load quickly.

Usage: python trim_tmdb.py TMDB_movie_dataset_v11.csv tmdb-tv.csv [rows]

Pass --tmdb-key=KEY (or set TMDB_API_KEY) to also look up each title's YouTube
trailer and store it in a trailer_key column. Without a key that column is left
empty and the app reports that no trailer is available.
"""
import io
import itertools
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

VIDEOS_URL = "https://api.themoviedb.org/3/movie/{id}/videos"
# Best trailer first: real trailers over teasers, official over fan uploads, big over small.
TYPE_RANK = {"trailer": 0, "teaser": 1, "clip": 2}
WORKERS = 8  # TMDB tolerates well over this; keeps us clear of its rate limit


def env_file_key():
    """TMDB_API_KEY out of scripts/.env or ./.env, tolerating spaces, quotes and comments."""
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(here, ".env"), os.path.join(os.getcwd(), ".env")):
        if not os.path.isfile(path):
            continue
        with io.open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, _, value = line.partition("=")
                if name.strip().replace("export ", "").strip() == "TMDB_API_KEY":
                    return value.strip().strip("\"'").strip()
    return ""


def parse_args(argv):
    key = os.environ.get("TMDB_API_KEY", "") or env_file_key()
    rest = []
    for a in argv:
        if a.startswith("--tmdb-key="):
            key = a.split("=", 1)[1]
        else:
            rest.append(a)
    if len(rest) < 2:
        sys.exit("Usage: python trim_tmdb.py <source.csv> <out.csv> [rows] [--tmdb-key=KEY]")
    return rest[0], rest[1], int(rest[2]) if len(rest) > 2 else 600, key.strip()


def trailer_key(movie_id, key):
    """The YouTube id of the best trailer for one movie, or '' when there is none."""
    url = VIDEOS_URL.format(id=movie_id) + "?language=en-US"
    headers = {}
    if key.startswith("ey"):  # v4 read access tokens are JWTs and go in a header
        headers["Authorization"] = "Bearer " + key
    else:                     # v3 keys go in the query string
        url += "&api_key=" + key

    results = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as r:
                results = json.load(r).get("results", [])
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:  # rate limited: back off and retry
                time.sleep(2 * (attempt + 1))
                continue
            return ""
        except Exception:
            return ""
    if not results:
        return ""

    videos = [v for v in results if v.get("site") == "YouTube" and v.get("key")]
    if not videos:
        return ""
    videos.sort(key=lambda v: (
        TYPE_RANK.get(str(v.get("type", "")).lower(), 3),
        not v.get("official"),
        -int(v.get("size") or 0),
    ))
    return videos[0]["key"]


def add_trailers(df, key):
    ids = df.id.tolist()
    done = itertools.count(1)

    def fetch(movie_id):
        found = trailer_key(movie_id, key)
        n = next(done)
        if n % 100 == 0:
            print(f"  looked up {n}/{len(ids)}", flush=True)
        return found

    print(f"Looking up trailers for {len(ids)} titles...", flush=True)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        keys = list(pool.map(fetch, ids))
    df["trailer_key"] = keys
    print(f"Found trailers for {sum(1 for k in keys if k)} of {len(ids)} titles")


src, out, rows, api_key = parse_args(sys.argv[1:])
cols = ["id", "title", "release_date", "runtime", "genres", "overview", "tagline",
        "poster_path", "backdrop_path", "vote_average", "vote_count", "popularity",
        "status", "adult"]
df = pd.read_csv(src, usecols=cols, dtype={"adult": "string"})
df = df[(df.status == "Released") & (df.adult.str.lower() != "true")]
df = df.dropna(subset=["title", "overview", "poster_path", "backdrop_path", "genres", "release_date"])
df = df[(df.vote_count >= 300) & (df.runtime > 0)]
df = df[pd.to_datetime(df.release_date, errors="coerce") <= pd.Timestamp.today()]
df = df.sort_values("popularity", ascending=False).drop_duplicates("id").head(rows)
df["vote_average"] = df.vote_average.round(1)
df["popularity"] = df.popularity.round(1)

if api_key:
    add_trailers(df, api_key)
else:
    df["trailer_key"] = ""
    print("No TMDB key given, so trailer_key is empty. Pass --tmdb-key=KEY to fill it.")

df.drop(columns=["status", "adult"]).to_csv(out, index=False)
print(f"Wrote {len(df)} titles to {out}")
