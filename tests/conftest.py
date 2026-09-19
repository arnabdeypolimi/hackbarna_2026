"""Keep the unit suite off the developer's real data/ and off the network.

A populated .env makes `create_app()` build a real Runtime; embedded Qdrant
is single-process and the history DB is the demo's, so every test gets its
own throw-away data directory and the deterministic stub agent."""
import pytest

from tv_avatar.catalog import AvatarProfile, LanguageProfile, SessionPersona
from tv_avatar.config import get_settings

#: A persona that never touches avatars.yaml, for tests of the session and
#: pipeline layers that do not care which avatar is speaking.
PERSONA = SessionPersona(
    avatar=AvatarProfile(id="test", name="Test", anam_avatar_id="avatar-1", voice="voice-1"),
    language=LanguageProfile(code="en", name="English", native_name="English"),
)


@pytest.fixture(autouse=True)
def _isolated_runtime_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_IMPL", "stub")
    monkeypatch.setenv("CATALOG_PATH", str(tmp_path / "catalog.parquet"))
    monkeypatch.setenv("QDRANT_PATH", str(tmp_path / "qdrant_db"))
    monkeypatch.setenv("HISTORY_DB_PATH", str(tmp_path / "history.db"))
    monkeypatch.setenv("MEMORY_ROOT", str(tmp_path / "memory"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
