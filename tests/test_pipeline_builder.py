from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transcriptions.language import Language

from conftest import PERSONA
from tv_avatar.agent.llm import build_llm
from tv_avatar.catalog import AvatarProfile
from tv_avatar.config import Settings
from tv_avatar.pipeline.services import build_anam, build_stt, build_tts
from tv_avatar.pipeline.turns import RESON8_TTFS_P99_S

VOICE = "f786b574-daa5-4673-aa0c-cbe3e8534c02"


def _settings(**over):
    base = {
        "nebius_api_key": "nb-test",
        "slng_api_key": "sk-test",
        "anam_api_key": "anam-test",
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
    assert stt._settings.language == Language.EN


def test_stt_language_is_forwarded_verbatim():
    # SLNG sends str(Language) in the connection config; "ca" must not be
    # remapped or dropped on the way to Reson8.
    stt = build_stt(_settings(), Language.CA)
    assert stt._settings.language == Language.CA
    assert str(stt._settings.language) == "ca"


async def test_stt_coalesces_audio_under_slng_message_rate_limit(monkeypatch):
    """SLNG closes the socket past 2000 msgs/min; 20 ms WebRTC frames are 50/s."""
    from pipecat.frames.frames import VADUserStoppedSpeakingFrame
    from pipecat.processors.frame_processor import FrameDirection
    from pipecat_slng import SlngSTTService

    sent: list[int] = []

    async def fake_run_stt(self, audio):
        sent.append(len(audio))
        yield None

    monkeypatch.setattr(SlngSTTService, "run_stt", fake_run_stt)
    stt = build_stt(_settings())
    stt._sample_rate = 16000                    # what setup() derives from the pipeline
    frame_20ms = b"\0" * 640
    for _ in range(10):                          # 200 ms of audio
        async for _ in stt.run_stt(frame_20ms):
            pass
    assert sent == [1920, 1920, 1920]            # 60 ms sends -> ~17 msgs/s, not 50

    async def no_parent(frame, direction):       # the tail is flushed before `finalize`
        pass

    monkeypatch.setattr(SlngSTTService, "process_frame", lambda self, f, d: no_parent(f, d))
    await stt.process_frame(VADUserStoppedSpeakingFrame(stop_secs=0.2), FrameDirection.DOWNSTREAM)
    assert sent == [1920, 1920, 1920, 640]


def test_tts_uses_configured_voice_encoding_and_region():
    tts = build_tts(_settings(), VOICE)
    url, headers, init = tts._connection_options()
    # Cartesia sonic-3 contract: linear16 @ 24 kHz, voice in the init message.
    assert url == "wss://api.slng.ai/v1/bridges/unmute/tts/cartesia/sonic:3"
    assert headers["X-World-Part-Override"] == "eu"
    assert f'"voice": "{VOICE}"' in init
    assert '"encoding": "linear16"' in init
    assert '"language": "en"' in init
    assert tts._encoding == "linear16"

    custom = build_tts(_settings(slng_tts_model="slng/x/tts:1"), "voice-z", Language.ES)
    assert custom._settings.model == "slng/x/tts:1"
    assert custom._settings.voice == "voice-z"
    assert '"language": "es"' in custom._connection_options()[2]


def test_llm_targets_nebius_token_factory():
    llm = build_llm(_settings())
    assert isinstance(llm, OpenAILLMService)
    assert llm._settings.model == "Qwen/Qwen3-30B-A3B-Instruct-2507"
    assert "tokenfactory.nebius.com" in str(llm._client.base_url)


def test_anam_enables_audio_passthrough_and_disables_replay():
    # Passthrough means our TTS drives the avatar rather than Anam's own
    # voice; replay off keeps Anam from recording the session (spec §4).
    anam = build_anam(_settings(video_width=640, video_height=960), PERSONA.avatar)
    assert anam._persona_config.avatar_id == "avatar-1"
    assert anam._persona_config.avatar_model == "cara-4"
    assert anam._persona_config.enable_audio_passthrough is True
    assert anam._enable_session_replay is False
    assert (anam._video_width, anam._video_height) == (640, 960)
    # Regression: leaving this None produced https://api.anam.ai/None/engine/session.
    assert anam._api_version == "v1"


def test_env_overrides_apply_to_the_default_avatar_only():
    """Anam avatar ids are account-scoped: the shared yaml's Cara can be missing
    under a teammate's key, so .env may substitute their own avatar or persona."""
    from tv_avatar.catalog import get_catalog
    from tv_avatar.pipeline.services import anam_persona

    default = get_catalog().avatar(get_catalog().default_avatar)
    other = AvatarProfile(id="other", name="O", anam_avatar_id="avatar-9", voice="v")

    plain = anam_persona(_settings(), default)
    assert plain.avatar_id == default.anam_avatar_id and plain.persona_id is None

    by_avatar = anam_persona(_settings(anam_avatar_id="mine-1"), default)
    assert by_avatar.avatar_id == "mine-1" and by_avatar.avatar_model == default.anam_avatar_model

    by_persona = anam_persona(_settings(anam_avatar_id="mine-1", anam_persona_id="persona-1"), default)
    assert by_persona.persona_id == "persona-1" and by_persona.avatar_id is None  # persona wins

    untouched = anam_persona(_settings(anam_avatar_id="mine-1", anam_persona_id="persona-1"), other)
    assert untouched.avatar_id == "avatar-9" and untouched.persona_id is None


def test_anam_persona_comes_from_the_avatar_profile_not_settings():
    other = AvatarProfile(id="b", name="B", anam_avatar_id="avatar-2",
                          anam_avatar_model="cara-5", voice="v")
    anam = build_anam(_settings(), other)
    assert anam._persona_config.avatar_id == "avatar-2"
    assert anam._persona_config.avatar_model == "cara-5"
