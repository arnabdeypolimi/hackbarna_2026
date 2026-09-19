"""SmallWebRTC transport for one browser peer (spec D5)."""
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

from tv_avatar.config import Settings


def build_transport(
    connection: SmallWebRTCConnection,
    settings: Settings,
    *,
    with_avatar: bool = True,
) -> SmallWebRTCTransport:
    # Anam resamples our TTS to whatever the transport emits, so the output
    # rate is pinned here rather than inherited from the TTS (spec §13).
    return SmallWebRTCTransport(
        webrtc_connection=connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_out_sample_rate=settings.slng_tts_sample_rate,
            video_out_enabled=with_avatar,
            video_out_is_live=with_avatar,
            video_out_width=settings.video_width,
            video_out_height=settings.video_height,
            video_out_framerate=settings.target_fps,
        ),
    )
