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
    assert s.slng_stt_model == "reson8/reson8stt:v1"
    assert s.slng_tts_model == "cartesia/sonic:3"
    assert s.slng_tts_encoding == "linear16"
    assert s.slng_tts_sample_rate == 24000


def test_missing_required_key_fails_fast(monkeypatch):
    for key in REQUIRED:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_blank_required_key_is_rejected(monkeypatch):
    # `.env.example` ships every key blank; copying it must not pass validation.
    _set_required(monkeypatch, NEBIUS_API_KEY="")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
