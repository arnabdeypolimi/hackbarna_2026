import json
from types import SimpleNamespace

from tv_avatar.config import Settings
from tv_avatar.memory.summary_lane import (
    EMPTY_PROFILE,
    PENDING_FILE,
    PROFILE_FILE,
    PROFILE_SCHEMA,
    SESSIONS_DIR,
    SUMMARY_SYSTEM,
    ProfileDraft,
    SummaryMemoryLane,
    render_profile,
    verified_facts,
)


def _draft(*facts: dict) -> str:
    """A decoded `ProfileDraft`, as the constrained decoder returns it."""
    return json.dumps({"facts": [{"source": "viewer_said", "section": "preferences", **f} for f in facts]})


class FakeLLM:
    """Returns a draft; the lane verifies it and renders the Markdown itself."""

    def __init__(self, reply: str | None = None, fail: bool = False) -> None:
        self.reply = reply if reply is not None else _draft(
            {"evidence": "something slow and sad", "fact": "seems to prefer slow, moody dramas"})
        self.fail, self.calls = fail, []
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
    llm = FakeLLM(json.dumps({"facts": [
        {"source": "viewer_said", "evidence": "I hate horror", "section": "dislikes",
         "fact": "said once that they hate horror"},
        {"source": "previous_profile", "evidence": "likes sci-fi", "section": "preferences",
         "fact": "likes sci-fi"}]}))
    lane, llm = _lane(tmp_path, llm)
    (tmp_path / "mem" / "u1").mkdir(parents=True)
    (tmp_path / "mem" / "u1" / PROFILE_FILE).write_text("## Preferences\n- likes sci-fi\n")
    await lane.ingest_turn("u1", "I hate horror", "Noted.")
    await lane.finish_session("u1")

    (call,) = llm.calls
    assert call["messages"][0]["content"] == SUMMARY_SYSTEM.format(max_words=200)
    assert call["response_format"]["json_schema"]["name"] == "viewer_profile"
    user_msg = call["messages"][1]["content"]
    assert "likes sci-fi" in user_msg and "Viewer: I hate horror" in user_msg and "Assistant: Noted." in user_msg
    # Rendered from the verified facts, in section order — the file stays editable Markdown.
    written = (tmp_path / "mem" / "u1" / PROFILE_FILE).read_text().strip()
    assert written == "## Preferences\n- likes sci-fi\n## Dislikes\n- said once that they hate horror"
    assert not (tmp_path / "mem" / "u1" / PENDING_FILE).exists()
    assert len(list((tmp_path / "mem" / "u1" / SESSIONS_DIR).iterdir())) == 1
    block = await lane.recall("u1", "anything")
    assert block.render_for_prompt() == written and not block.empty


async def test_a_fact_the_viewer_never_said_does_not_reach_the_profile(tmp_path):
    """The contamination path: the assistant offered animated adventures and the
    profile came back claiming the viewer likes them, plus an instruction to keep
    offering them. Neither is supported by a viewer line, so neither is written."""
    llm = FakeLLM(json.dumps({"facts": [
        {"source": "viewer_said", "evidence": "colorful animated adventures", "section": "preferences",
         "fact": "responded positively to animated action adventures"},
        {"source": "previous_profile", "evidence": "", "section": "open_threads",
         "fact": "should offer similar picks with a fun, colorful style"},
        {"source": "viewer_said", "evidence": "what should I watch", "section": "open_threads",
         "fact": "asked what to watch and did not pick anything yet"}]}))
    lane, llm = _lane(tmp_path, llm)
    await lane.ingest_turn("u1", "What should I watch?",
                           "Let me find some colorful animated adventures. Try Elemental.")
    await lane.finish_session("u1")
    profile = (tmp_path / "mem" / "u1" / PROFILE_FILE).read_text()
    assert profile.strip() == "## Open threads\n- asked what to watch and did not pick anything yet"
    assert "animated" not in profile and "should offer" not in profile


async def test_a_session_with_nothing_durable_writes_the_empty_profile(tmp_path):
    lane, _ = _lane(tmp_path, FakeLLM(json.dumps({"facts": []})))
    await lane.ingest_turn("u1", "hello", "Hi there!")
    await lane.finish_session("u1")
    assert (tmp_path / "mem" / "u1" / PROFILE_FILE).read_text().strip() == EMPTY_PROFILE


async def test_an_undecodable_draft_keeps_the_transcript_instead_of_writing_prose(tmp_path):
    """Before the schema the summariser's free text was written verbatim; now a
    reply that is not a draft is a failed fold, and the turns wait for a retry."""
    lane, _ = _lane(tmp_path, FakeLLM("## Preferences\n- loved the pacing of Suzume"))
    await lane.ingest_turn("u1", "I hate horror", "Noted.")
    await lane.finish_session("u1")
    assert not (tmp_path / "mem" / "u1" / PROFILE_FILE).exists()
    assert (tmp_path / "mem" / "u1" / PENDING_FILE).exists()


def test_evidence_is_matched_against_the_source_it_claims():
    draft = ProfileDraft.model_validate({"facts": [
        {"source": "viewer_said", "evidence": "Something slow and sad, please!", "section": "preferences",
         "fact": "asked for something slow and sad"},
        {"source": "viewer_said", "evidence": "colorful animated adventures", "section": "preferences",
         "fact": "likes animated adventures"},
        {"source": "previous_profile", "evidence": "dislikes jump scares", "section": "dislikes",
         "fact": "dislikes jump scares"},
        {"source": "previous_profile", "evidence": "invented earlier line", "section": "preferences",
         "fact": "likes westerns"},
    ]})
    kept = verified_facts(draft, viewer_text="something slow and sad please",
                          previous="## Dislikes\n- dislikes jump scares")
    assert [f.fact for f in kept] == ["asked for something slow and sad", "dislikes jump scares"]
    assert render_profile(kept) == ("## Preferences\n- asked for something slow and sad\n"
                                    "## Dislikes\n- dislikes jump scares")


def test_a_fact_that_instructs_the_assistant_is_dropped_whatever_its_source():
    directives = ["should offer similar picks", "always recommend animated adventures",
                  "offer more colorful films next time", "make sure to suggest sequels"]
    draft = ProfileDraft.model_validate({"facts": [
        {"source": "previous_profile", "evidence": d, "section": "open_threads", "fact": d}
        for d in directives]})
    assert verified_facts(draft, viewer_text="", previous="\n".join(directives)) == []
    assert render_profile([]) == EMPTY_PROFILE


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
    import os
    import time
    (d / PROFILE_FILE).write_text("## Preferences\n- v2 (edited by hand)\n")
    os.utime(d / PROFILE_FILE, (time.time() + 5, time.time() + 5))
    assert "v2" in (await lane.recall("u1", "b")).render_for_prompt()


def test_summary_schema_requires_a_source_and_its_evidence_before_the_fact():
    schema = PROFILE_SCHEMA["schema"]
    assert PROFILE_SCHEMA["name"] == "viewer_profile" and PROFILE_SCHEMA["strict"] is True
    fact = schema["$defs"]["ProfileFact"]
    assert list(fact["properties"]) == ["source", "evidence", "section", "fact"]
    assert fact["properties"]["source"]["enum"] == ["viewer_said", "previous_profile"]
    assert fact["properties"]["section"]["enum"] == [
        "preferences", "dislikes", "how_to_talk", "open_threads"]
    assert schema["properties"]["facts"]["maxItems"] == 12


def test_summary_prompt_allows_signal_titles_but_not_assistant_sourced_facts():
    prompt = SUMMARY_SYSTEM.format(max_words=200)
    assert "Titles only when they carry a signal" in prompt
    assert "never record a title the assistant merely suggested" in prompt
    assert "What the assistant\n  said is never evidence" in prompt


def test_summary_instructions_have_no_example_movie_preferences_to_copy():
    assert "Suzume" not in SUMMARY_SYSTEM
    assert "The Nun" not in SUMMARY_SYSTEM
    assert "Do not infer from greetings" in SUMMARY_SYSTEM


def test_summary_rules_keep_facts_without_recommending_future_actions():
    prompt = SUMMARY_SYSTEM.format(max_words=200)
    assert "a fact whose evidence is not found there is discarded" in prompt
    assert "never an instruction to the assistant" in prompt
    assert 'Do not\n  write "should offer"' in prompt
    assert "Drop previous-profile lines that are instructions" in prompt
    assert "turn talk about the room, the interface or debugging into film preferences" in prompt


async def test_recalling_a_legacy_profile_does_not_rewrite_it(tmp_path):
    from tv_avatar.agent.prompt import volatile_sections

    lane, llm = _lane(tmp_path)
    path = tmp_path / "mem" / "u1" / PROFILE_FILE
    path.parent.mkdir(parents=True)
    legacy = "## Open threads\nShould offer similar animated picks.\n"
    path.write_text(legacy)
    block = await lane.recall("u1", "Watch Moon.")
    system = volatile_sections("View: grid", block.render_for_prompt(), "none", "none")
    encoded = system.split("# Memory\n")[1].split("\n\n# Recent activity")[0]
    assert json.loads(encoded)["viewer_profile"] == legacy.strip()
    assert path.read_text() == legacy
    assert llm.calls == []


async def test_profile_refreshes_mid_session_every_n_turns(tmp_path):
    """Facts from this session reach the prompt before the session ends: after
    MEMORY_REFRESH_EVERY_TURNS ingests the pending transcript is folded into the
    profile off the turn, and the next recall sees it."""
    import asyncio

    llm = FakeLLM(_draft({"evidence": "something funny tonight",
                          "fact": "said once they wanted something funny tonight"}))
    settings = Settings(nebius_api_key="x", slng_api_key="-", anam_api_key="-", anam_avatar_id="-",
                        _env_file=None, memory_root=str(tmp_path / "mem"), memory_refresh_every_turns=3)
    lane = SummaryMemoryLane(settings, client=llm)
    for i in range(2):
        await lane.ingest_turn("u1", f"turn {i}", "ok")
    assert llm.calls == []
    await lane.ingest_turn("u1", "something funny tonight", "Let me look.")
    await asyncio.sleep(0.05)   # the refresh is fire-and-forget
    assert len(llm.calls) == 1
    assert "something funny tonight" in llm.calls[0]["messages"][1]["content"]
    assert "funny tonight" in (await lane.recall("u1", "x")).render_for_prompt()
    assert not (tmp_path / "mem" / "u1" / PENDING_FILE).exists()
    # The counter restarts: two more turns do not trigger another summary.
    for i in range(2):
        await lane.ingest_turn("u1", f"later {i}", "ok")
    await asyncio.sleep(0.05)
    assert len(llm.calls) == 1


async def test_refresh_is_off_by_default_and_when_zero(tmp_path):
    import asyncio

    lane, llm = _lane(tmp_path)
    assert lane._refresh_every == 6
    settings = Settings(nebius_api_key="x", slng_api_key="-", anam_api_key="-", anam_avatar_id="-",
                        _env_file=None, memory_root=str(tmp_path / "mem2"), memory_refresh_every_turns=0)
    off = SummaryMemoryLane(settings, client=llm)
    for i in range(10):
        await off.ingest_turn("u1", f"turn {i}", "ok")
    await asyncio.sleep(0.05)
    assert llm.calls == []


async def test_turns_ingested_while_summarising_are_not_lost(tmp_path):
    """finish_session reads N pending turns, then the LLM takes a while; a turn
    appended meanwhile must stay pending for the next fold, not vanish into the archive."""
    import asyncio

    class SlowLLM(FakeLLM):
        async def _create(self, **kwargs):
            await asyncio.sleep(0.1)
            return await super()._create(**kwargs)

    lane, llm = _lane(tmp_path, SlowLLM())
    await lane.ingest_turn("u1", "first", "ok")
    folding = asyncio.create_task(lane.finish_session("u1"))
    await asyncio.sleep(0.02)                      # summariser in flight
    await lane.ingest_turn("u1", "second", "ok")   # appended mid-summary
    await folding
    assert "Viewer: first" in llm.calls[0]["messages"][1]["content"]
    assert "second" not in llm.calls[0]["messages"][1]["content"]
    pending = (tmp_path / "mem" / "u1" / PENDING_FILE).read_text().splitlines()
    assert [json.loads(p)["user"] for p in pending] == ["second"]
    archived = list((tmp_path / "mem" / "u1" / SESSIONS_DIR).iterdir())
    assert len(archived) == 1 and "second" not in archived[0].read_text()


async def test_finish_session_span_is_a_generation_carrying_the_session_baggage(tmp_path, otel):
    from conftest import PERSONA

    from tv_avatar.session.state import SessionState
    from tv_avatar.tracing import session_scope

    lane, llm = _lane(tmp_path, FakeLLM(_draft(
        {"evidence": "I hate horror", "section": "dislikes", "fact": "said once that they hate horror"})))
    session = SessionState("sess_9", "tok", 0, persona=PERSONA, user_id="u1")
    with session_scope(session, Settings(nebius_api_key="x", slng_api_key="-", anam_api_key="-", _env_file=None)):
        await lane.ingest_turn("u1", "I hate horror", "Noted.")
        await lane.finish_session("u1")
    fold, = otel.spans()["memory.finish_session"]
    assert fold.attributes["langfuse.observation.type"] == "generation"
    assert fold.attributes["langfuse.observation.metadata.trigger"] == "session_end"
    assert fold.attributes["langfuse.session.id"] == "sess_9"
    assert fold.attributes["gen_ai.request.model"] == lane._model
    assert fold.attributes["tv.memory.turns"] == 1
    assert "Viewer: I hate horror" in fold.attributes["langfuse.observation.input"]
    # The span records what was written, not the draft: dropped facts never existed.
    assert fold.attributes["langfuse.observation.output"] == "## Dislikes\n- said once that they hate horror"
    assert llm.calls[0]["response_format"]["json_schema"]["name"] == "viewer_profile"


async def test_mid_session_fold_is_detached_from_the_turn_but_keeps_the_session(tmp_path, otel):
    import asyncio

    from conftest import PERSONA

    from tv_avatar.session.state import SessionState
    from tv_avatar.tracing import observation, session_scope

    settings = Settings(nebius_api_key="x", slng_api_key="-", anam_api_key="-", anam_avatar_id="-",
                        _env_file=None, memory_root=str(tmp_path / "mem"), memory_refresh_every_turns=2)
    lane = SummaryMemoryLane(settings, client=FakeLLM())
    session = SessionState("sess_9", "tok", 0, persona=PERSONA, user_id="u1")
    with session_scope(session, settings), observation("llm", type="generation") as llm_span:
        await lane.ingest_turn("u1", "turn 0", "ok")
        await lane.ingest_turn("u1", "turn 1", "ok")
        await asyncio.sleep(0.05)
    spans = otel.spans()
    first, second = spans["memory.ingest"]
    assert first.parent.span_id == llm_span.context.span_id
    assert first.attributes["langfuse.observation.metadata.refresh_triggered"] is False
    assert second.attributes["langfuse.observation.metadata.refresh_triggered"] is True
    assert second.attributes["tv.memory.pending_turns"] == 2
    fold, = spans["memory.finish_session"]
    assert fold.attributes["langfuse.observation.metadata.trigger"] == "every_n_turns"
    assert fold.parent is None                                   # detached from the turn
    assert fold.attributes["langfuse.session.id"] == "sess_9"    # baggage survived the detach


async def test_failed_summary_marks_the_span_and_keeps_the_transcript(tmp_path, otel):
    lane, _ = _lane(tmp_path, FakeLLM(fail=True))
    await lane.ingest_turn("u1", "I hate horror", "Noted.")
    await lane.finish_session("u1")
    fold, = otel.spans()["memory.finish_session"]
    assert fold.status.status_code.name == "ERROR"
    assert fold.attributes["langfuse.observation.status_message"] == "RuntimeError"
    assert [e.name for e in fold.events] == ["transcript kept for retry"]
