"""The starter clips: the words are the browser's, verbatim; the tool talks to fal's queue
the way the queue expects and never hands the key to the file host."""
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import make_ambient_starters as tool
from tv_avatar.ambient.scenes import SCENES


def test_every_scene_the_model_can_ask_for_has_its_words():
    text = tool.load_scene_text()
    assert set(text) == {s.id for s in SCENES}
    for entry in text.values():
        assert entry["label"] and entry["beats"]
        assert "Audio:" in entry["prompt"]  # the sound is asked for in the same prompt


def test_request_sends_the_prompt_verbatim_and_unexpanded():
    assert tool.request_for("a fire") == {
        "prompt": "a fire", "duration": 15, "resolution": "1080P",
        "aspect_ratio": "16:9", "prompt_expansion_mode": "disabled",
    }


def test_make_submits_waits_for_completion_and_downloads_the_file(tmp_path):
    seen: list[tuple[str, str, str | None]] = []
    states = iter(["IN_QUEUE", "IN_PROGRESS", "COMPLETED"])
    queue = f"{tool.QUEUE}/{tool.ENDPOINT}"
    video_url = "https://v3.fal.media/files/x/fire.mp4"

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append((request.method, url, request.headers.get("authorization")))
        if request.method == "POST" and url == queue:
            assert json.loads(request.content)["prompt"] == "a fire"
            return httpx.Response(200, json={
                "request_id": "r1",
                "status_url": f"{queue}/requests/r1/status",
                "response_url": f"{queue}/requests/r1",
            })
        if url == f"{queue}/requests/r1/status":
            return httpx.Response(200, json={"status": next(states)})
        if url == f"{queue}/requests/r1":
            return httpx.Response(200, json={"video": {"url": video_url, "content_type": "video/mp4"}})
        if url == video_url:
            return httpx.Response(200, content=b"\x00\x00\x00\x18ftypisom fake mp4")
        return httpx.Response(404)

    out = tmp_path / "fireplace.mp4"
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        tool.make(client, tool.request_for("a fire"), out, auth={"Authorization": "Key k"},
                  poll_s=0, log=lambda _: None)

    assert out.read_bytes().startswith(b"\x00\x00\x00\x18ftyp")
    assert [m for m, _, _ in seen] == ["POST", "GET", "GET", "GET", "GET", "GET"]
    assert all(a == "Key k" for _, u, a in seen if u.startswith(tool.QUEUE))
    assert all(a is None for _, u, a in seen if not u.startswith(tool.QUEUE))


def test_dry_run_prints_the_request_and_sends_nothing(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(tool, "OUT_DIR", tmp_path)
    assert tool.main(["--dry-run", "--scene", "beach"]) == 0
    printed = capsys.readouterr().out
    assert '"scene": "beach"' in printed and "waves" in printed.lower()
    assert not list(tmp_path.iterdir())


def test_a_starter_already_made_is_kept_unless_forced(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(tool, "OUT_DIR", tmp_path)
    (tmp_path / "beach.mp4").write_bytes(b"x")
    assert tool.main(["--dry-run", "--scene", "beach"]) == 0
    assert "exists" in capsys.readouterr().out
    assert tool.main(["--dry-run", "--scene", "beach", "--force"]) == 0
    assert '"scene": "beach"' in capsys.readouterr().out
