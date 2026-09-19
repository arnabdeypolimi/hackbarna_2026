"""Construct the third-party services from configuration.

Voice, speed and language are pinned for the session: changing any of
them mid-session forces a SLNG WebSocket reconnect, heard as a gap.
"""
from collections.abc import AsyncGenerator

from anam import PersonaConfig
from pipecat.frames.frames import Frame, VADUserStoppedSpeakingFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.transcriptions.language import Language
from pipecat_anam import AnamVideoService
from pipecat_slng import SlngSTTService, SlngTTSService

from tv_avatar.catalog import AvatarProfile
from tv_avatar.config import Settings
from tv_avatar.pipeline.turns import RESON8_TTFS_P99_S

#: SLNG closes the STT socket (1008) past 2000 messages/minute, i.e. ~33/s.
#: WebRTC hands us 20 ms audio frames — 50/s — so a session tripped the limit
#: ~40 s in and lost audio across the reconnect. 60 ms per send is ~17/s,
#: leaving room for finalize/keepalive, at a latency cost below one VAD tick.
STT_SEND_INTERVAL_MS = 60


class BatchedSlngSTTService(SlngSTTService):
    """SlngSTTService that coalesces audio into `STT_SEND_INTERVAL_MS` sends."""

    def __init__(self, *, send_interval_ms: int = STT_SEND_INTERVAL_MS, **kwargs) -> None:
        super().__init__(**kwargs)
        self._send_interval_ms = send_interval_ms
        self._pending = bytearray()

    @property
    def _bytes_per_send(self) -> int:
        rate = self.sample_rate or self._init_sample_rate or 16000  # sample_rate is 0 before setup()
        return rate * 2 * self._send_interval_ms // 1000  # 16-bit mono

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        self._pending += audio
        if len(self._pending) < self._bytes_per_send:
            yield None
            return
        async for frame in self._flush():
            yield frame

    async def _flush(self) -> AsyncGenerator[Frame | None, None]:
        if not self._pending:
            return
        chunk, self._pending = bytes(self._pending), bytearray()
        async for frame in super().run_stt(chunk):
            yield frame

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        if isinstance(frame, VADUserStoppedSpeakingFrame):
            # The tail of the utterance must be on the wire before `finalize`.
            await self.process_generator(self._flush())
        await super().process_frame(frame, direction)


def build_stt(settings: Settings, language: Language = Language.EN) -> SlngSTTService:
    return BatchedSlngSTTService(
        api_key=settings.slng_api_key,
        model=settings.slng_stt_model,
        base_url=settings.slng_base_url,
        world_part_override=settings.slng_world_part,
        language=language,
        enable_vad=True,
        enable_partials=True,
        ttfs_p99_latency=RESON8_TTFS_P99_S,
    )


def build_tts(settings: Settings, voice: str, language: Language = Language.EN) -> SlngTTSService:
    return SlngTTSService(
        api_key=settings.slng_api_key,
        model=settings.slng_tts_model,
        voice=voice,
        base_url=settings.slng_base_url,
        world_part_override=settings.slng_world_part,
        encoding=settings.slng_tts_encoding,
        sample_rate=settings.slng_tts_sample_rate,
        language=language,
    )


def build_anam(settings: Settings, avatar: AvatarProfile) -> AnamVideoService:
    # pipecat-anam 0.2.0a6: `enable_session_replay` is a service kwarg, not a
    # PersonaConfig field (verified against the installed signatures).
    # `api_version` must be given explicitly: the plugin forwards its None
    # default over the SDK's "v1", yielding ".../None/engine/session" (404).
    return AnamVideoService(
        api_key=settings.anam_api_key,
        api_version="v1",
        persona_config=PersonaConfig(
            avatar_id=avatar.anam_avatar_id,
            avatar_model=avatar.anam_avatar_model,
            enable_audio_passthrough=True,
        ),
        enable_session_replay=False,
        video_width=settings.video_width,
        video_height=settings.video_height,
    )
