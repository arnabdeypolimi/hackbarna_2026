from pipecat.services.openai.llm import OpenAILLMService

from tv_avatar.agent.llm import build_llm
from tv_avatar.config import Settings
from tv_avatar.pipeline.services import build_anam, build_stt, build_tts
from tv_avatar.pipeline.turns import RESON8_TTFS_P99_S


def _settings(**over):
    base = {
        "nebius_api_key": "nb-test",
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
    stt = build_stt(_settings(slng_stt_model="slng/x/stt:1", slng_world_part="na"))
    assert stt._settings.model == "slng/x/stt:1"
    assert stt._base_url == "api.slng.ai"
    assert stt._world_part_override == "na"
    assert stt._settings.enable_vad is True
    assert stt._settings.enable_partials is True
    assert stt._ttfs_p99_latency == RESON8_TTFS_P99_S


def test_tts_uses_configured_voice_encoding_and_region():
    tts = build_tts(_settings())
    url, headers, init = tts._connection_options()
    # Cartesia sonic-3 contract: linear16 @ 24 kHz, voice in the init message.
    assert url == "wss://api.slng.ai/v1/bridges/unmute/tts/cartesia/sonic:3"
    assert headers["X-World-Part-Override"] == "eu"
    assert '"voice": "f786b574-daa5-4673-aa0c-cbe3e8534c02"' in init
    assert '"encoding": "linear16"' in init
    assert tts._encoding == "linear16"

    custom = build_tts(_settings(slng_tts_model="slng/x/tts:1", slng_tts_voice="voice-z"))
    assert custom._settings.model == "slng/x/tts:1"
    assert custom._settings.voice == "voice-z"


def test_llm_targets_nebius_token_factory():
    llm = build_llm(_settings())
    assert isinstance(llm, OpenAILLMService)
    assert llm._settings.model == "Qwen/Qwen3-30B-A3B-Instruct-2507"
    assert "tokenfactory.nebius.com" in str(llm._client.base_url)


def test_anam_enables_audio_passthrough_and_disables_replay():
    # Passthrough means our TTS drives the avatar rather than Anam's own
    # voice; replay off keeps Anam from recording the session (spec §4).
    anam = build_anam(_settings(video_width=640, video_height=960))
    assert anam._persona_config.avatar_id == "avatar-1"
    assert anam._persona_config.avatar_model == "cara-4"
    assert anam._persona_config.enable_audio_passthrough is True
    assert anam._enable_session_replay is False
    assert (anam._video_width, anam._video_height) == (640, 960)
    # Regression: leaving this None produced https://api.anam.ai/None/engine/session.
    assert anam._api_version == "v1"


def test_anam_persona_id_is_passed_as_persona_not_avatar():
    anam = build_anam(_settings(anam_avatar_id="", anam_persona_id="persona-1"))
    assert anam._persona_config.persona_id == "persona-1"
    assert anam._persona_config.avatar_id is None
