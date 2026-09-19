"""Construct the third-party services from configuration.

Voice, speed and language are pinned for the session: changing any of
them mid-session forces a SLNG WebSocket reconnect, heard as a gap.
"""
from anam import PersonaConfig
from pipecat.transcriptions.language import Language
from pipecat_anam import AnamVideoService
from pipecat_slng import SlngSTTService, SlngTTSService

from tv_avatar.config import Settings
from tv_avatar.pipeline.turns import RESON8_TTFS_P99_S


def build_stt(settings: Settings) -> SlngSTTService:
    return SlngSTTService(
        api_key=settings.slng_api_key,
        model=settings.slng_stt_model,
        base_url=settings.slng_base_url,
        world_part_override=settings.slng_world_part,
        language=Language.EN,
        enable_vad=True,
        enable_partials=True,
        ttfs_p99_latency=RESON8_TTFS_P99_S,
    )


def build_tts(settings: Settings) -> SlngTTSService:
    return SlngTTSService(
        api_key=settings.slng_api_key,
        model=settings.slng_tts_model,
        voice=settings.slng_tts_voice,
        base_url=settings.slng_base_url,
        world_part_override=settings.slng_world_part,
        encoding=settings.slng_tts_encoding,
        sample_rate=settings.slng_tts_sample_rate,
        language=Language.EN,
    )


def build_anam(settings: Settings) -> AnamVideoService:
    # pipecat-anam 0.2.0a6: `enable_session_replay` is a service kwarg, not a
    # PersonaConfig field (verified against the installed signatures).
    # `api_version` must be given explicitly: the plugin forwards its None
    # default over the SDK's "v1", yielding ".../None/engine/session" (404).
    return AnamVideoService(
        api_key=settings.anam_api_key,
        api_version="v1",
        persona_config=PersonaConfig(
            avatar_id=settings.anam_avatar_id,
            avatar_model=settings.anam_avatar_model,
            enable_audio_passthrough=True,
        ),
        enable_session_replay=False,
        video_width=settings.video_width,
        video_height=settings.video_height,
    )
