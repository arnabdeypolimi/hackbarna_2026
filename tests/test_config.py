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


def test_missing_required_key_fails_fast(monkeypatch):
    monkeypatch.delenv("SLNG_API_KEY", raising=False)
    monkeypatch.delenv("ANAM_API_KEY", raising=False)
    monkeypatch.delenv("ANAM_AVATAR_ID", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
