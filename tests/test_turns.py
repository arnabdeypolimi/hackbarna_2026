from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InputAudioRawFrame,
)
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)

from tv_avatar.pipeline.turns import (
    PHANTOM_TURN_TIMEOUT_S,
    MuteWhileBotSpeakingUserMuteStrategy,
    user_aggregator_params,
)


async def test_half_duplex_mutes_only_while_bot_speaks():
    strategy = MuteWhileBotSpeakingUserMuteStrategy()
    audio = InputAudioRawFrame(audio=b"\0\0", sample_rate=16000, num_channels=1)
    assert await strategy.process_frame(audio) is False
    assert await strategy.process_frame(BotStartedSpeakingFrame()) is True
    assert await strategy.process_frame(audio) is True
    assert await strategy.process_frame(BotStoppedSpeakingFrame()) is False


def test_full_duplex_is_the_default():
    params = user_aggregator_params()
    assert params.user_mute_strategies == []
    assert params.user_turn_stop_timeout == PHANTOM_TURN_TIMEOUT_S
    assert params.vad_analyzer is not None


def test_turn_ends_on_a_silence_timer_not_the_smart_turn_model():
    params = user_aggregator_params(turn_silence_s=0.7)
    (stop,) = params.user_turn_strategies.stop
    assert isinstance(stop, SpeechTimeoutUserTurnStopStrategy)
    assert stop._user_speech_timeout == 0.7
    assert stop.wait_for_transcript is True  # the LLM still needs words


def test_settings_carry_the_silence_threshold(monkeypatch):
    from tv_avatar.config import Settings
    for k, v in {"NEBIUS_API_KEY": "x", "SLNG_API_KEY": "x",
                 "ANAM_API_KEY": "x", "ANAM_AVATAR_ID": "x", "TURN_SILENCE_S": "0.8"}.items():
        monkeypatch.setenv(k, v)
    assert Settings(_env_file=None).turn_silence_s == 0.8


def test_half_duplex_installs_the_mute_strategy():
    params = user_aggregator_params(half_duplex=True)
    assert any(isinstance(s, MuteWhileBotSpeakingUserMuteStrategy)
               for s in params.user_mute_strategies)
