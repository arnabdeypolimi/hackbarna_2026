from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    TTSTextFrame,
)
from pipecat.observers.base_observer import FramePushed
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.tests.utils import run_test

from tv_avatar.pipeline.echo import (
    BotSpeechObserver,
    EchoTranscriptFilter,
    SpokenWindow,
)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _window(clock: Clock | None = None) -> SpokenWindow:
    return SpokenWindow(clock=clock or Clock())


def _speaking(window: SpokenWindow, *sentences: str) -> SpokenWindow:
    window.bot_started()
    for s in sentences:
        window.add(s)
    return window


def test_avatar_sentence_heard_back_is_echo():
    w = _speaking(_window(), "How about a heist movie tonight?")
    assert w.is_echo("how about a heist movie tonight")
    assert w.is_echo("About a heist movie")  # partial, as interims arrive


def test_viewer_talking_over_the_avatar_is_not_echo():
    w = _speaking(_window(), "How about a heist movie tonight?")
    assert not w.is_echo("no wait, show me comedies instead")
    assert not w.is_echo("stop")


def test_known_false_positive_a_correction_that_reuses_a_title_is_called_echo():
    """Why the guard is off by default (ECHO_FILTER). Field log 2026-09-20: the
    viewer's "no die hard" shares "no" and "hard" with the title the avatar had
    just named, 2/3 ≥ the ratio. Re-enable the guard knowing this."""
    w = _speaking(_window(), "I found a few.",
                  "Carl's Date is a 2023 animated adventure, Inside Out from 2015 is a family "
                  "story about feelings, and No Hard Feelings is a 2023 comedy romance.")
    assert w.is_echo("no die hard")
    assert not w.is_echo("die hard")


def test_nothing_is_echo_while_the_avatar_is_silent():
    w = _window()
    w.add("How about a heist movie tonight?")
    assert not w.is_echo("how about a heist movie tonight")


def test_echo_still_counts_briefly_after_the_avatar_stops():
    """STT trails the audio, so the last words come back after playback ended."""
    clock = Clock()
    w = _speaking(_window(clock), "Enjoy the film.")
    w.bot_stopped()
    clock.now += 1.0
    assert w.is_echo("enjoy the film")
    clock.now += 5.0
    assert not w.is_echo("enjoy the film")


def test_spoken_words_expire():
    clock = Clock()
    w = _speaking(_window(clock), "Enjoy the film.")
    clock.now += 60
    assert w.words() == set()


async def test_observer_feeds_the_window():
    w = _window()
    obs = BotSpeechObserver(w)
    src = FrameProcessor()
    for frame in (BotStartedSpeakingFrame(), TTSTextFrame(text="Sleep well tonight.", aggregated_by="sentence")):
        await obs.on_push_frame(FramePushed(source=src, destination=src, frame=frame,
                                            direction=FrameDirection.DOWNSTREAM, timestamp=0))
    assert w.is_echo("sleep well tonight")
    await obs.on_push_frame(FramePushed(source=src, destination=src, frame=BotStoppedSpeakingFrame(),
                                        direction=FrameDirection.DOWNSTREAM, timestamp=0))
    assert not w._speaking


async def test_filter_drops_echo_and_passes_the_viewer():
    w = _speaking(_window(), "How about a heist movie tonight?")
    flt = EchoTranscriptFilter(w)
    echo = InterimTranscriptionFrame(text="how about a heist movie", user_id="u", timestamp="0")
    viewer = TranscriptionFrame(text="show me comedies instead", user_id="u", timestamp="0")
    await run_test(flt, frames_to_send=[echo, viewer], expected_down_frames=[TranscriptionFrame])
    assert flt.dropped == 1
