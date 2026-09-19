import json
from types import SimpleNamespace

from tv_avatar.config import Settings
from tv_avatar.memory.summary_lane import (
    PENDING_FILE,
    PROFILE_FILE,
    SESSIONS_DIR,
    SUMMARY_SYSTEM,
    SummaryMemoryLane,
)


class FakeLLM:
    def __init__(self, reply: str = "## Preferences\n- slow, moody dramas", fail: bool = False) -> None:
        self.reply, self.fail, self.calls = reply, fail, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("endpoint down")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.reply))])


def _lane(tmp_path, llm=None) -> tuple[SummaryMemoryLane, FakeLLM]:
    llm = llm or FakeLLM()
    settings = Settings(nebius_api_key="x", slng_api_key="-", anam_api_key="-", anam_avatar_id="-",
                        _env_file=None, memory_root=str(tmp_path / "mem"))
    return SummaryMemoryLane(settings, client=llm), llm


async def test_turns_are_buffered_not_summarised_until_the_session_ends(tmp_path):
    lane, llm = _lane(tmp_path)
    await lane.ingest_turn("u1", "something slow and sad", "Let me look.")
    await lane.ingest_turn("u1", "no, forget that", "Got it.")
    assert llm.calls == []
    pending = (tmp_path / "mem" / "u1" / PENDING_FILE).read_text().splitlines()
    assert [json.loads(p)["user"] for p in pending] == ["something slow and sad", "no, forget that"]
    assert (await lane.recall("u1", "hi")).empty  # nothing consolidated yet


async def test_finish_session_rewrites_profile_from_previous_plus_transcript(tmp_path):
    lane, llm = _lane(tmp_path)
    (tmp_path / "mem" / "u1").mkdir(parents=True)
    (tmp_path / "mem" / "u1" / PROFILE_FILE).write_text("## Preferences\n- likes sci-fi\n")
    await lane.ingest_turn("u1", "I hate horror", "Noted.")
    await lane.finish_session("u1")

    (call,) = llm.calls
    assert call["messages"][0]["content"] == SUMMARY_SYSTEM.format(max_words=200)
    user_msg = call["messages"][1]["content"]
    assert "likes sci-fi" in user_msg and "Viewer: I hate horror" in user_msg and "Assistant: Noted." in user_msg
    assert (tmp_path / "mem" / "u1" / PROFILE_FILE).read_text().strip() == llm.reply
    assert not (tmp_path / "mem" / "u1" / PENDING_FILE).exists()
    assert len(list((tmp_path / "mem" / "u1" / SESSIONS_DIR).iterdir())) == 1
    block = await lane.recall("u1", "anything")
    assert block.render_for_prompt() == llm.reply and not block.empty


async def test_finish_session_with_nothing_pending_is_a_noop(tmp_path):
    lane, llm = _lane(tmp_path)
    await lane.finish_session("nobody")
    assert llm.calls == []


async def test_failed_summary_keeps_the_transcript_for_retry(tmp_path):
    lane, llm = _lane(tmp_path, FakeLLM(fail=True))
    await lane.ingest_turn("u1", "I hate horror", "Noted.")
    await lane.finish_session("u1")
    assert (tmp_path / "mem" / "u1" / PENDING_FILE).exists()
    llm.fail = False
    await lane.warmup()  # catch-up at next start folds it in
    assert not (tmp_path / "mem" / "u1" / PENDING_FILE).exists()
    assert (tmp_path / "mem" / "u1" / PROFILE_FILE).exists()


async def test_profile_is_reread_when_the_file_changes(tmp_path):
    lane, _ = _lane(tmp_path)
    d = tmp_path / "mem" / "u1"
    d.mkdir(parents=True)
    (d / PROFILE_FILE).write_text("## Preferences\n- v1\n")
    assert "v1" in (await lane.recall("u1", "a")).render_for_prompt()
    import os, time
    (d / PROFILE_FILE).write_text("## Preferences\n- v2 (edited by hand)\n")
    os.utime(d / PROFILE_FILE, (time.time() + 5, time.time() + 5))
    assert "v2" in (await lane.recall("u1", "b")).render_for_prompt()


def test_summary_prompt_allows_signal_titles_but_not_assistant_sourced_facts():
    prompt = SUMMARY_SYSTEM.format(max_words=200)
    assert "Titles are welcome when they carry a signal" in prompt
    assert "never write down a title the assistant merely suggested" in prompt
    assert "Nothing the assistant suggested is a fact" in prompt
