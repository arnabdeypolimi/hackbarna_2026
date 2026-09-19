# Anam interruption behaviour — M1 risk gate findings

**Date:** 2026-09-19
**Question (spec §9 rule 2, §13):** when the user barges in, does `AnamVideoService` stop
speaking, or does its startup buffer keep playing?
**Status:** static analysis complete; live measurement **pending credentials**.

## Static analysis of `pipecat-anam` 0.2.0a6 (installed, verified by `inspect.getsource`)

`AnamVideoService.process_frame` explicitly handles Pipecat's `InterruptionFrame`:

```python
if isinstance(frame, InterruptionFrame):
    await self._handle_interruption()
```

`_handle_interruption` does four things under a send-state lock:

1. `await self._anam_session.interrupt()` — tells Anam Cloud to stop the current utterance.
2. Cancels the internal send task that drains the TTS-audio queue to Anam.
3. `await self._agent_audio_stream.end_sequence()` — resets the SDK's audio chunk
   sequence number, so audio already queued but unsent is discarded rather than replayed.
4. Recreates a fresh send task (in `finally`) so the next turn starts clean.

The service also drops `TTSAudioRawFrame` from the downstream path
("Anam syncs TTS with video"), so there is no second audio buffer in the Pipecat
output transport to flush. The plugin's own docstring lists "Interrupt handling for more
natural conversations" as a feature.

**Interpretation:** the interrupt path exists and is deliberate. What static analysis
cannot answer is the *latency* between `InterruptionFrame` and the avatar visibly and
audibly stopping, which depends on Anam Cloud's server-side buffer and the WebRTC
round-trip.

## Live measurements — PENDING

Blocked on `SLNG_API_KEY`, `ANAM_API_KEY`, `ANAM_AVATAR_ID`. No `.env` was present when
this task ran. Fill in when keys land (plan Task 11, steps 4–5):

| # | Measurement | Result |
|---|---|---|
| M0 | Voice loop without avatar: transcripts appear, stub prose spoken, barge-in stops speech | _pending_ |
| 1 | Idle video keeps flowing between turns (spec §4 media contract) | _pending_ |
| 2 | Delivered frame rate vs 25 fps target | _pending_ |
| 3 | Barge-in → avatar stops within ~200 ms? Measured delay | _pending_ |
| 4 | If buffered audio continues: which hook was missing | n/a — hook exists, see above |

## Procedure for whoever runs it

```bash
cp .env.example .env   # fill keys
uv run pytest tests/ -v
# M0: run_session(..., with_avatar=False) against the mock TV client (Task 10)
# M1: run_session(..., with_avatar=True); speak, interrupt mid-sentence, time the stop
```

Record numbers in the table above and update **Status** at the top.
