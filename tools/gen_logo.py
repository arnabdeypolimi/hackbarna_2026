"""Generate logo candidates for Mira via the fal.ai queue API.

Usage: uv run python tools/gen_logo.py [--n 3] [--model fal-ai/flux-pro/v1.1-ultra]
Reads FAL_KEY from .env; writes PNGs next to the prompt under docs/screenshots/logo-candidates/.
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
PROMPT = ROOT / "docs/screenshots/logo-prompt.md"
OUT = ROOT / "docs/screenshots/logo-candidates"


def submit(client: httpx.Client, model: str, prompt: str) -> str:
    r = client.post(
        f"https://queue.fal.run/{model}",
        json={"prompt": prompt, "aspect_ratio": "1:1", "output_format": "png", "num_images": 1},
    )
    r.raise_for_status()
    return r.json()["response_url"]


def wait(client: httpx.Client, response_url: str) -> dict:
    status_url = response_url.replace("/requests/", "/requests/").rstrip("/") + "/status"
    while True:
        s = client.get(status_url).json()
        if s.get("status") == "COMPLETED":
            return client.get(response_url).json()
        if s.get("status") in {"FAILED", "ERROR"}:
            raise RuntimeError(s)
        time.sleep(2)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--model", default="fal-ai/flux-pro/v1.1-ultra")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    key = os.environ.get("FAL_KEY")
    if not key:
        raise SystemExit("FAL_KEY is not set")

    prompt = PROMPT.read_text()
    OUT.mkdir(parents=True, exist_ok=True)
    with httpx.Client(headers={"Authorization": f"Key {key}"}, timeout=120) as client:
        urls = [submit(client, args.model, prompt) for _ in range(args.n)]
        for i, url in enumerate(urls):
            result = wait(client, url)
            img = result["images"][0]["url"]
            path = OUT / f"mira-logo-{i}.png"
            path.write_bytes(httpx.get(img, timeout=120).content)
            logger.info("wrote {}", path)


if __name__ == "__main__":
    main()
