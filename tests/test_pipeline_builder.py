from tv_avatar.config import Settings
from tv_avatar.pipeline.services import build_anam, build_stt, build_tts


def _settings(**over):
    base = {
        "slng_api_key": "sk-test",
        "anam_api_key": "anam-test",
        "anam_avatar_id": "avatar-1",
        "_env_file": None,
    }
    base.update(over)
    return Settings(**base)


# These read private attributes of the installed plugins on purpose: the point
# of the smoke tests is to prove the constructor kwargs land where we think
# they do in *this* plugin version (the M0 risk). If a plugin bump renames
# them, that is exactly the signal we want.


def test_stt_uses_configured_model_and_region():
    stt = build_stt(_settings(slng_stt_model="slng/x/stt:1", slng_base_url="us.api.slng.ai"))
    assert stt._settings.model == "slng/x/stt:1"
    assert stt._base_url == "us.api.slng.ai"
    assert stt._settings.enable_vad is True
    assert stt._settings.enable_partials is True


def test_tts_uses_configured_voice():
    tts = build_tts(_settings(slng_tts_model="slng/x/tts:1", slng_tts_voice="voice-z"))
    assert tts._settings.model == "slng/x/tts:1"
    assert tts._settings.voice == "voice-z"
    assert tts._base_url == "eu.api.slng.ai"


def test_anam_enables_audio_passthrough_and_disables_replay():
    # Passthrough means our TTS drives the avatar rather than Anam's own
    # voice; replay off keeps Anam from recording the session (spec §4).
    anam = build_anam(_settings(video_width=640, video_height=960))
    assert anam._persona_config.avatar_id == "avatar-1"
    assert anam._persona_config.avatar_model == "cara-4"
    assert anam._persona_config.enable_audio_passthrough is True
    assert anam._enable_session_replay is False
    assert (anam._video_width, anam._video_height) == (640, 960)
