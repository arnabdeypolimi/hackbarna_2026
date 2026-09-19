"""Turn-taking configuration for the user aggregator.

Measured against Reson8 (2026-09-19): a ``finalize`` is answered in
~0.35 s, partials trail the audio by ~1.2 s, and without ``finalize`` the
final only arrives after ~3 s of silence. The numbers below follow from
that, not from Pipecat's defaults.
"""
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
)
from pipecat.processors.aggregators.llm_response_universal import (
    LLMUserAggregatorParams,
)
from pipecat.turns.user_mute.base_user_mute_strategy import BaseUserMuteStrategy
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies

#: Speech end -> final transcript, P99, once the plugin has sent ``finalize``.
RESON8_TTFS_P99_S = 0.5

#: Silence after the user's last word before the turn is handed to the LLM.
#: Replaces the smart-turn model: a plain timer is predictable and cannot
#: hold a turn open on a "sounds unfinished" verdict.
DEFAULT_TURN_SILENCE_S = 0.5

#: Silero's stop window. Kept at pipecat's recommended value; the silence
#: threshold above runs on top of it, so end-to-end endpointing is the sum.
VAD_STOP_SECS = 0.2

#: Barge-in is VAD-driven, so every Silero start cancels TTS. In a live room
#: (2026-09-19) cross-talk and noise started turns that never produced a word
#: and cut replies before their first audio byte. Pipecat defaults are
#: confidence 0.7 / start 0.2 s / volume 0.6 (~-50 LUFS); these demand a
#: sustained, clearly voiced, near-mic signal (~-40 LUFS) before interrupting.
VAD_CONFIDENCE = 0.85
VAD_START_SECS = 0.35
VAD_MIN_VOLUME = 0.7

#: How long a turn may stay open with VAD activity but no transcript before it
#: is abandoned. Speaker echo and room noise trip Silero without producing
#: words; the default 5 s left the avatar mute for that long.
PHANTOM_TURN_TIMEOUT_S = 2.0


class MuteWhileBotSpeakingUserMuteStrategy(BaseUserMuteStrategy):
    """Half-duplex: drop mic input while the avatar is talking.

    Disables barge-in, so it is a diagnostic aid for laptop-speaker setups
    where the avatar's own voice re-enters the microphone, not the product
    behaviour (spec §9).
    """

    def __init__(self) -> None:
        super().__init__()
        self._bot_speaking = False

    async def process_frame(self, frame: Frame) -> bool:
        await super().process_frame(frame)
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
        return self._bot_speaking


def user_aggregator_params(
    *,
    turn_silence_s: float = DEFAULT_TURN_SILENCE_S,
    half_duplex: bool = False,
) -> LLMUserAggregatorParams:
    """Turn-taking: VAD start, silence-timer stop.

    The turn closes ``VAD_STOP_SECS + turn_silence_s`` after the last word,
    once at least one transcript has arrived. Reson8's final lands ~0.55 s
    after speech end (finalize at VAD stop + 0.35 s), so at the default
    threshold the text is normally already in hand when the timer fires.
    """
    return LLMUserAggregatorParams(
        vad_analyzer=SileroVADAnalyzer(
            params=VADParams(confidence=VAD_CONFIDENCE, start_secs=VAD_START_SECS,
                             stop_secs=VAD_STOP_SECS, min_volume=VAD_MIN_VOLUME),
        ),
        user_turn_strategies=UserTurnStrategies(
            stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=turn_silence_s)],
        ),
        user_turn_stop_timeout=PHANTOM_TURN_TIMEOUT_S,
        user_mute_strategies=[MuteWhileBotSpeakingUserMuteStrategy()] if half_duplex else [],
    )
