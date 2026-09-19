import pytest
from pydantic import ValidationError

from tv_avatar.config import Settings

REQUIRED = {
    "NEBIUS_API_KEY": "nb-test",
    "SLNG_API_KEY": "sk-test",
    "ANAM_API_KEY": "anam-test",
    "ANAM_AVATAR_ID": "avatar-1",
}


def _set_required(monkeypatch, **overrides):
    for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "NEBIUS_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {**REQUIRED, **overrides}.items():
        monkeypatch.setenv(key, value)


def test_settings_load_from_env(monkeypatch):
    _set_required(monkeypatch)
    s = Settings(_env_file=None)
    assert s.slng_api_key == "sk-test"
    assert s.slng_base_url == "api.slng.ai"
    assert s.slng_world_part == "eu"
    assert s.video_width == 768
    assert s.target_fps == 25


def test_stack_defaults_match_the_chosen_providers(monkeypatch):
    _set_required(monkeypatch)
    s = Settings(_env_file=None)
    assert s.nebius_base_url.startswith("https://api.tokenfactory.nebius.com/v1")
    assert s.llm_model == "Qwen/Qwen3-30B-A3B-Instruct-2507"
    assert s.slng_stt_model == "deepgram/nova:3"
    assert s.slng_tts_model == "cartesia/sonic:3"
    assert s.slng_tts_encoding == "linear16"
    assert s.slng_tts_sample_rate == 24000


def test_missing_required_key_fails_fast(monkeypatch):
    for key in (*REQUIRED, "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_blank_required_key_is_rejected(monkeypatch):
    # `.env.example` ships every key blank; copying it must not pass validation.
    _set_required(monkeypatch, NEBIUS_API_KEY="")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


# --- phase 2 -----------------------------------------------------------------

def test_phase2_defaults(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.delenv("AGENT_IMPL", raising=False)  # conftest pins stub for the suite
    s = Settings(_env_file=None)
    assert s.agent_impl == "sgr"
    assert s.tool_timeout_s == 0.4
    assert s.qdrant_path.endswith("qdrant_db")
    assert s.embedding_model == "Qwen/Qwen3-Embedding-8B"
    assert s.llm_extra_body == {"chat_template_kwargs": {"enable_thinking": False}}


def test_openai_env_names_are_accepted_as_aliases(monkeypatch):
    """VoiceMem/mem0 and the OpenAI SDK speak OPENAI_*; a .env written for them still works."""
    _set_required(monkeypatch)
    monkeypatch.delenv("NEBIUS_API_KEY")
    monkeypatch.setenv("OPENAI_API_KEY", "nb-alias")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.test/v1/")
    s = Settings(_env_file=None)
    assert s.nebius_api_key == "nb-alias"
    assert s.nebius_base_url == "https://example.test/v1/"


def test_llm_extra_body_parses_json_env(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("LLM_EXTRA_BODY", '{"chat_template_kwargs":{"enable_thinking":false}}')
    assert Settings(_env_file=None).llm_extra_body == {"chat_template_kwargs": {"enable_thinking": False}}


def test_anam_persona_id_replaces_avatar_id(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.delenv("ANAM_AVATAR_ID")
    monkeypatch.setenv("ANAM_PERSONA_ID", "persona-1")
    assert Settings(_env_file=None).anam_persona_id == "persona-1"
    monkeypatch.delenv("ANAM_PERSONA_ID")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
