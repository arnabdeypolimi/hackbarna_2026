"""Render the starter clips the ambient scenes open with.

A spoken "show me the fireplace" puts a scene on screen at once: the starter made here
plays while fal Director makes the full minute behind it (frontend/src/ambient). The
starters are one short loop per scene from the same prompt, rendered by fal's queue
endpoint for the same model, which returns a file rather than a stream, so this needs no
browser. They ship with the app in frontend/public/ambient/ (Git LFS), so a TV has them
before any session is opened.

Run: uv run python tools/make_ambient_starters.py [--scene fireplace] [--force] [--dry-run]

Each clip is 15 s of 1080p video with the sound the model makes for it. fal bills the
endpoint per second of video (about $0.08/s at 768p at the time of writing; 1080p is
listed on fal.ai/models/minimax/h3-max/text-to-video), so a full run is a few dollars.
FAL_KEY comes from .env, as it does for the theme agent.
"""
import argparse
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from tv_avatar.ambient.scenes import SCENES
from tv_avatar.theme_agent.settings import ThemeAgentSettings

ROOT = Path(__file__).resolve().parents[1]
#: The words, shared with the browser's Director session so both describe the same scene.
SCENES_JSON = ROOT / "frontend" / "src" / "ambient" / "scenes.json"
OUT_DIR = ROOT / "frontend" / "public" / "ambient"
#: The queue form of the model the browser streams from: it renders to a file.
ENDPOINT = "minimax/h3-max/text-to-video"
QUEUE = "https://queue.fal.run"
#: The endpoint's longest clip. A starter only has to cover the wait for the session's first
#: frame, but a longer loop wraps less often.
DURATION_S = 15
RESOLUTION = "1080P"
#: A render can queue behind other jobs; past this something is wrong.
WAIT_S = 15 * 60


def load_scene_text() -> dict[str, dict]:
    """The JSON keyed by scene id, checked against the ids the model may ask for."""
    text = json.loads(SCENES_JSON.read_text(encoding="utf-8"))
    ids = {s.id for s in SCENES}
    if set(text) != ids:
        raise SystemExit(f"scenes.json has {sorted(text)} but the backend offers {sorted(ids)}")
    return text


def request_for(prompt: str, *, duration: int = DURATION_S, resolution: str = RESOLUTION,
                expansion: str = "disabled") -> dict:
    """The prompt goes verbatim, as Director gets it, so the starter and the clip that
    follows it describe the same scene; expansion would rewrite it."""
    return {
        "prompt": prompt,
        "duration": duration,
        "resolution": resolution,
        "aspect_ratio": "16:9",
        "prompt_expansion_mode": expansion,
    }


def make(client: httpx.Client, request: dict, out: Path, *, auth: dict[str, str],
         poll_s: float = 3.0, wait_s: float = WAIT_S,
         log: Callable[[str], None] = print) -> Path:
    """Submit, wait, download. Raises on any answer that is not the file.

    `auth` goes only to the queue: the file comes from fal's media host, which has no
    business seeing the key."""
    submitted = client.post(f"{QUEUE}/{ENDPOINT}", json=request, headers=auth)
    submitted.raise_for_status()
    body = submitted.json()
    request_id = body["request_id"]
    status_url = body.get("status_url") or f"{QUEUE}/{ENDPOINT}/requests/{request_id}/status"
    response_url = body.get("response_url") or f"{QUEUE}/{ENDPOINT}/requests/{request_id}"
    log(f"  submitted {request_id}")

    deadline = time.monotonic() + wait_s
    while True:
        status = client.get(status_url, headers=auth)
        status.raise_for_status()
        state = status.json().get("status")
        if state == "COMPLETED":
            break
        if time.monotonic() > deadline:
            raise TimeoutError(f"fal did not finish {request_id} within {wait_s:.0f} s (last: {state})")
        time.sleep(poll_s)

    result = client.get(response_url, headers=auth)
    result.raise_for_status()
    video = result.json()["video"]
    out.parent.mkdir(parents=True, exist_ok=True)
    with client.stream("GET", video["url"]) as download, out.open("wb") as fh:
        download.raise_for_status()
        for chunk in download.iter_bytes():
            fh.write(chunk)
    log(f"  {out.name}: {out.stat().st_size / 1e6:.1f} MB")
    return out


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--scene", action="append", choices=[s.id for s in SCENES],
                    help="one scene (repeatable); default: every scene without a starter")
    ap.add_argument("--duration", type=int, default=DURATION_S, help="seconds, 5-15")
    ap.add_argument("--resolution", default=RESOLUTION, choices=["480P", "768P", "1080P"])
    ap.add_argument("--expansion", default="disabled", choices=["disabled", "balanced", "quality"],
                    help="fal's prompt rewriting; off so the starter matches the Director prompt")
    ap.add_argument("--force", action="store_true", help="remake a starter that exists")
    ap.add_argument("--dry-run", action="store_true", help="print the requests and stop")
    args = ap.parse_args(argv)

    text = load_scene_text()
    jobs: list[tuple[str, dict, Path]] = []
    for sid in args.scene or [s.id for s in SCENES]:
        out = OUT_DIR / f"{sid}.mp4"
        if out.exists() and not args.force:
            print(f"{sid}: {_rel(out)} exists (--force remakes it)")
            continue
        jobs.append((sid, request_for(text[sid]["prompt"], duration=args.duration,
                                      resolution=args.resolution, expansion=args.expansion), out))
    if not jobs:
        return 0
    print(f"{len(jobs)} clip(s) of {args.duration} s at {args.resolution}; "
          f"fal bills per second (fal.ai/models/{ENDPOINT})")
    if args.dry_run:
        for sid, req, out in jobs:
            print(json.dumps({"scene": sid, "out": _rel(out), "request": req}, indent=2))
        return 0

    settings = ThemeAgentSettings()
    if settings.fal_key_problem:
        print(settings.fal_key_problem, file=sys.stderr)
        return 2
    auth = {"Authorization": f"Key {settings.fal_key}"}
    with httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
        for sid, req, out in jobs:
            print(f"{sid}:")
            make(client, req, out, auth=auth)
    print(f"done: {_rel(OUT_DIR)} (tracked with Git LFS, see .gitattributes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
