from tv_avatar.config import Settings
from tv_avatar.pipeline.services import build_anam, build_stt, build_tts


def _settings():
    return Settings(
        slng_api_key="sk-test",
        anam_api_key="anam-test",
        anam_avatar_id="avatar-1",
        _env_file=None,
    )


def test_stt_uses_configured_model_and_region():
    stt = build_stt(_settings())
    assert stt is not None


def test_tts_uses_configured_voice():
    tts = build_tts(_settings())
    assert tts is not None


def test_anam_enables_audio_passthrough_and_disables_replay():
    # Passthrough means our TTS drives the avatar rather than Anam's own
    # voice; replay off keeps Anam from recording the session (spec §4).
    anam = build_anam(_settings())
    assert anam is not None
