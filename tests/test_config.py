import pytest
from pydantic import ValidationError

from tv_avatar.config import Settings


def test_settings_load_from_env(monkeypatch):
    monkeypatch.setenv("SLNG_API_KEY", "sk-test")
    monkeypatch.setenv("ANAM_API_KEY", "anam-test")
    monkeypatch.setenv("ANAM_AVATAR_ID", "avatar-1")
    s = Settings(_env_file=None)
    assert s.slng_api_key == "sk-test"
    assert s.slng_base_url == "eu.api.slng.ai"
    assert s.video_width == 768
    assert s.target_fps == 25


def test_phase2_defaults(monkeypatch):
    monkeypatch.setenv("SLNG_API_KEY", "sk")
    monkeypatch.setenv("ANAM_API_KEY", "an")
    monkeypatch.setenv("ANAM_AVATAR_ID", "av")
    monkeypatch.setenv("OPENAI_API_KEY", "nb-test")
    s = Settings(_env_file=None)
    assert s.openai_api_key == "nb-test"
    assert s.openai_base_url.startswith("https://api.tokenfactory.nebius.com")
    assert s.agent_impl == "stub"
    assert s.tool_timeout_s == 0.4
    assert s.qdrant_path.endswith("qdrant_db")
    assert s.llm_extra_body == {"chat_template_kwargs": {"enable_thinking": False}}


def test_llm_extra_body_parses_json_env(monkeypatch):
    monkeypatch.setenv("SLNG_API_KEY", "sk")
    monkeypatch.setenv("ANAM_API_KEY", "an")
    monkeypatch.setenv("ANAM_AVATAR_ID", "av")
    monkeypatch.setenv("LLM_EXTRA_BODY", "{}")
    assert Settings(_env_file=None).llm_extra_body == {}


def test_missing_required_key_fails_fast(monkeypatch):
    monkeypatch.delenv("SLNG_API_KEY", raising=False)
    monkeypatch.delenv("ANAM_API_KEY", raising=False)
    monkeypatch.delenv("ANAM_AVATAR_ID", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
