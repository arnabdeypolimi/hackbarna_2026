"""Suppress the avatar's own voice coming back through the microphone.

Browser echo cancellation is the first line of defence and Pipecat leaves it
there (no server-side AEC). On TV and laptop speakers the residual is still
intelligible: Safari and Chrome both returned the avatar's sentences as *user*
transcripts (2026-09-20), so VAD thresholds and word counts cannot tell them
apart. What can is the text itself — we know exactly what the avatar said, and
a "user" utterance that repeats it while echo is physically possible is echo.

Two pieces share one ``SpokenWindow``:

- ``BotSpeechObserver`` records TTS text and the speaking interval. An
  observer, so it never sits in the media path.
- ``EchoTranscriptFilter`` sits between STT and the user aggregator and drops
  transcripts whose words mostly come from that window.

Opt-in via ``ECHO_FILTER`` and off by default: word overlap cannot tell echo
from a correction that reuses the avatar's words — "no, die hard" straight
after it named "No Hard Feelings" scored 2/3 and was dropped (2026-09-20).
"""
import re
import time
from collections import deque
from collections.abc import Callable

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    TTSTextFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

#: Transcripts keep arriving after playback ends: partials trail the audio by
#: ~1.2 s and the final follows finalize by ~0.5 s (turns.py), so echo of the
#: avatar's last words can only surface inside this window after it stops.
ECHO_GRACE_S = 2.0

#: How long spoken text stays matchable. TTS runs ahead of playback and a reply
#: is one to two sentences, so this comfortably covers "said, then heard back".
SPOKEN_TTL_S = 20.0

#: Share of a transcript's words that must be in the spoken window to call it
#: echo. Echo transcribes near-verbatim; a viewer talking over the avatar
#: rarely repeats more than an odd word of it.
ECHO_MATCH_RATIO = 0.6

_WORD = re.compile(r"\w+", re.UNICODE)


def words_of(text: str) -> list[str]:
    return _WORD.findall(text.casefold())


class SpokenWindow:
    """What the avatar said recently, and whether its echo can still arrive."""

    def __init__(self, *, ttl_s: float = SPOKEN_TTL_S, grace_s: float = ECHO_GRACE_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl_s, self._grace_s, self._clock = ttl_s, grace_s, clock
        self._said: deque[tuple[float, frozenset[str]]] = deque()
        self._speaking = False
        self._stopped_at: float | None = None

    def add(self, text: str) -> None:
        if words := words_of(text):
            self._said.append((self._clock(), frozenset(words)))

    def bot_started(self) -> None:
        self._speaking, self._stopped_at = True, None

    def bot_stopped(self) -> None:
        self._speaking, self._stopped_at = False, self._clock()

    def echo_possible(self) -> bool:
        if self._speaking:
            return True
        return self._stopped_at is not None and self._clock() - self._stopped_at <= self._grace_s

    def words(self) -> set[str]:
        cutoff = self._clock() - self._ttl_s
        while self._said and self._said[0][0] < cutoff:
            self._said.popleft()
        return set().union(*(w for _, w in self._said))

    def is_echo(self, text: str, *, ratio: float = ECHO_MATCH_RATIO) -> bool:
        if not self.echo_possible():
            return False
        heard = words_of(text)
        if not heard:
            return False
        said = self.words()
        return sum(w in said for w in heard) / len(heard) >= ratio


class BotSpeechObserver(BaseObserver):
    """Feed the window from the frames TTS and the output transport emit.

    Frames are seen once per hop; every update here is idempotent so no
    de-duplication is needed.
    """

    def __init__(self, window: SpokenWindow) -> None:
        super().__init__()
        self._window = window

    async def on_push_frame(self, data: FramePushed) -> None:
        match data.frame:
            case TTSTextFrame(text=text):
                self._window.add(text)
            case BotStartedSpeakingFrame():
                self._window.bot_started()
            case BotStoppedSpeakingFrame():
                self._window.bot_stopped()


class EchoTranscriptFilter(FrameProcessor):
    """Drop user transcripts that are the avatar hearing itself."""

    def __init__(self, window: SpokenWindow, *, ratio: float = ECHO_MATCH_RATIO, **kwargs) -> None:
        super().__init__(**kwargs)
        self._window, self._ratio = window, ratio
        self.dropped = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if (isinstance(frame, (InterimTranscriptionFrame, TranscriptionFrame))
                and self._window.is_echo(frame.text, ratio=self._ratio)):
            self.dropped += 1
            logger.info("echo transcript dropped: {!r}", frame.text)
            return
        await self.push_frame(frame, direction)
