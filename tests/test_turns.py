from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)

from tv_avatar.pipeline.turns import (
    PHANTOM_TURN_TIMEOUT_S,
    MuteWhileBotSpeakingUserMuteStrategy,
    WordsToBargeInUserTurnStartStrategy,
    user_aggregator_params,
)


def _interim(text: str) -> InterimTranscriptionFrame:
    return InterimTranscriptionFrame(text=text, user_id="u", timestamp="0")


def _start_strategy(min_words: int = 2) -> tuple[WordsToBargeInUserTurnStartStrategy, list]:
    strategy = WordsToBargeInUserTurnStartStrategy(min_words=min_words)
    started: list = []

    @strategy.event_handler("on_user_turn_started")
    async def _on_started(_strategy, *args, **kwargs):
        started.append(True)

    return strategy, started


async def test_vad_alone_starts_a_turn_while_the_avatar_is_silent():
    strategy, started = _start_strategy()
    assert await strategy.process_frame(VADUserStartedSpeakingFrame()) is ProcessFrameResult.STOP
    assert started == [True]


async def test_vad_alone_does_not_interrupt_the_avatar():
    """Speaker echo trips Silero; without words it must not cut the reply (2026-09-20)."""
    strategy, started = _start_strategy()
    await strategy.process_frame(BotStartedSpeakingFrame())
    assert await strategy.process_frame(VADUserStartedSpeakingFrame()) is ProcessFrameResult.CONTINUE
    assert await strategy.process_frame(_interim("the")) is ProcessFrameResult.CONTINUE
    assert started == []


async def test_enough_words_interrupt_the_avatar():
    strategy, started = _start_strategy(min_words=2)
    await strategy.process_frame(BotStartedSpeakingFrame())
    assert await strategy.process_frame(_interim("wait stop")) is ProcessFrameResult.STOP
    assert started == [True]


async def test_vad_start_works_again_once_the_avatar_stops():
    strategy, started = _start_strategy()
    await strategy.process_frame(BotStartedSpeakingFrame())
    await strategy.process_frame(BotStoppedSpeakingFrame())
    assert await strategy.process_frame(VADUserStartedSpeakingFrame()) is ProcessFrameResult.STOP
    assert started == [True]


def test_barge_in_word_threshold_is_configurable():
    params = user_aggregator_params(barge_in_min_words=3)
    (start,) = params.user_turn_strategies.start
    assert isinstance(start, WordsToBargeInUserTurnStartStrategy)
    assert start._min_words == 3


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


def test_vad_is_stricter_than_pipecat_defaults():
    """Every VAD start cancels TTS; room noise must not qualify (2026-09-19 field log)."""
    from pipecat.audio.vad.vad_analyzer import VAD_CONFIDENCE, VAD_MIN_VOLUME, VAD_START_SECS
    vad = user_aggregator_params().vad_analyzer.params
    assert vad.confidence > VAD_CONFIDENCE
    assert vad.start_secs > VAD_START_SECS
    assert vad.min_volume > VAD_MIN_VOLUME
    assert vad.min_volume <= 0.7  # ~-40 LUFS: a quiet viewer must still get through


def test_turn_ends_on_a_silence_timer_not_the_smart_turn_model():
    params = user_aggregator_params(turn_silence_s=0.7)
    (stop,) = params.user_turn_strategies.stop
    assert isinstance(stop, SpeechTimeoutUserTurnStopStrategy)
    assert stop._user_speech_timeout == 0.7
    assert stop.wait_for_transcript is True  # the LLM still needs words


def test_settings_carry_the_silence_threshold(monkeypatch):
    from tv_avatar.config import Settings
    for k, v in {"NEBIUS_API_KEY": "x", "SLNG_API_KEY": "x",
                 "ANAM_API_KEY": "x", "ANAM_AVATAR_ID": "x", "TURN_SILENCE_S": "0.8",
                 "BARGE_IN_MIN_WORDS": "3"}.items():
        monkeypatch.setenv(k, v)
    settings = Settings(_env_file=None)
    assert settings.turn_silence_s == 0.8
    assert settings.barge_in_min_words == 3


def test_half_duplex_installs_the_mute_strategy():
    params = user_aggregator_params(half_duplex=True)
    assert any(isinstance(s, MuteWhileBotSpeakingUserMuteStrategy)
               for s in params.user_mute_strategies)
