# Ambient scenes

Say "show me a relaxing fireplace video" to the avatar and a scene fills the screen at once:
a fire in a snowy cabin, waves on a beach, or birds in a spring garden, with its own sound,
looping until you say "close it" or press Back. What plays first is a **starter clip** shipped
with the app; behind it fal's **MiniMax H3 Max Director** makes a full minute of the scene,
which the TV keeps and plays the next time it is asked for. There is never a connecting screen.

The scenes and their prompts are the brief's (`relaxing-video-prompts.md`), verbatim, in
[scenes.json](scenes.json) — read by [scenes.ts](scenes.ts) for the Director session and by
`tools/make_ambient_starters.py` for the starters, so both describe the same scene. The ids
the model may choose from are the backend's (`src/tv_avatar/ambient/scenes.py`), read here
through the generated contract.

## How it works

```
"show me a fireplace" ──▶ voice agent ──▶ show_ambient {scene: "fireplace"}   (control socket)
                                                     │
                     App.tsx: withAmbient(handler) ──▶ useAmbient.show()
                                          │                       │
              kept clip? ── yes ──▶ IndexedDB ──▶ <video loop>    │ no
                                                                  ▼
                       public/ambient/fireplace.mp4 ──▶ <video loop>   (the starter, at once)
                                                                  │ meanwhile
                                                 AmbientSceneAgent (agent.ts)
                                                   fal.realtime.open(wma("minimax/h3-max/director"))
                                                                  ▼
                                    theme agent proxy ──▶ fal (adds the key)   [src/tv_avatar/theme_agent]
                                                                  ▲
                             WebRTC video + audio, recorded 60 s ─┘ ──▶ IndexedDB, for the next request
```

- **The starter plays at once.** One 15 s loop per scene, rendered from the same prompt by
  fal's queue endpoint for the same model (`minimax/h3-max/text-to-video`, which returns a file)
  and shipped in `public/ambient/` under Git LFS. Make them once:

  ```bash
  uv run python tools/make_ambient_starters.py            # every scene without a starter
  uv run python tools/make_ambient_starters.py --dry-run  # the requests, no charge
  ```

  fal bills the render per second of video (a few dollars for all three at 1080p). A scene
  whose starter is missing, or that the set cannot decode, falls back to the live path below.
- **The full clip is made behind it, once.** Director is a live stream, billed at $0.08/s with
  a 60 s minimum (at least **$4.80 a session**). The first request for a scene opens one,
  records a minute of it off screen, and keeps it; the starter keeps playing for that viewing
  rather than cutting to the new picture mid-scene. Every later request plays the kept clip
  and opens no session — asking for the scene again while the starter is still up, once the
  clip is kept, switches to it. Up to 4 clips are kept. With no starter, the live stream is
  shown as it arrives and the kept clip takes over when it is made, as the weather does.
- **With sound.** Director makes the audio in the same pass as the video, on its own track,
  and the prompt describes the sound as well as the picture. The recording keeps both. A
  spoken command is not the gesture a browser wants before it plays sound, so the scene may
  start muted with "Press OK for sound"; any key press is enough. If a session ever comes back
  silent, the clip is marked so (the note says "made without sound" rather than offering OK),
  and the fix is to pin a recording with Director's `audio_url` in `configureFor()`.
- **Nothing is made unasked.** Only the verb starts a session. Closing a scene while it is still
  being made lets the recording finish off screen and keeps it — the minute is billed anyway.
  Asking for another scene meanwhile closes the first session and starts the new one.
- **Closing.** Back on the remote, the spoken "close it" / "turn it off" (`hide_ambient`), or
  `close`, `back`, `home` and `play` — the scene is the topmost thing on screen, so those take it
  down first, as Back does. OK unmutes and brings the title back; play/pause holds the picture.
- **The room and the trailer.** The season's sky and any weather backdrop are paused while a
  scene is up (a set has one decoder), and told to decide for themselves again when it closes.
  A trailer that was playing is paused before the scene opens, so the two never talk over each
  other.
- **The agent knows.** The screen state carries `ambient: "fireplace"` while a scene is up and
  the prompt renders it on its own line, so "stop" means `hide_ambient` and not "nothing is
  playing".

## Run it

```bash
# 1. the fal proxy (the theme agent, shared with the weather backdrops); FAL_KEY in .env
uv run uvicorn tv_avatar.theme_agent.app:create_theme_agent_app --factory --port 8010

# 2. the avatar backend and the TV app, as usual
uv run uvicorn tv_avatar.app:app --reload --port 8000
cd frontend && npm run dev
```

`VITE_AMBIENT_PROXY_URL` (or the weather's `VITE_WEATHER_PROXY_URL`) points the app at the
proxy when it is not on `localhost:8010`. The proxy's daily session cap
(`THEME_AGENT_MAX_SESSIONS_PER_DAY`) covers scenes and weather together. If a scene fails, the
note on screen says why, using the proxy's `/health`; the table in
[../weather/README.md](../weather/README.md) has the finer diagnosis.

**Without a key:** open the app with `?ambientMock=1&ambientSeconds=6`. A canvas that draws the
scene, with a low tone for its sound, stands in for fal, and everything else — the command, the
fullscreen scene, recording with audio, keeping, looping, closing — is the real code. Add
`&ambientStarter=0` to watch the mock's own picture instead of the starter.

**Without the avatar either:** add `&ambientScene=fireplace` (or `beach`, `garden`) and the
scene opens on load as if it had been asked for. Back (Backspace or Escape on a desktop)
closes it; reload with the same flag and the kept clip plays with no session. Without
`ambientMock=1` that flag opens a real, paid session.

## What it touches outside this folder

| File | Change |
| --- | --- |
| `src/App.tsx` | an import, `useAmbient()`, the handler wrapped in `withAmbient(...)`, the screen state passed through `useAmbientScreen`, and one `<AmbientScene />` |
| `src/hooks/useTvControl.ts` | the two verbs added to `CommandHandler` |
| `../../src/tv_avatar/agent/commands.py` | the two verbs (the vocabulary's single source of truth), with the scene ids in `../../src/tv_avatar/ambient/` |
| `../../src/tv_avatar/agent/envelope.py`, `prompt.py` | their manifest lines and one rule |
| `../../src/tv_avatar/control/protocol.py`, `session/state.py` | `ScreenState.ambient`, optional, and its prompt line |
| `../../contracts/` | regenerated |
| `../../.gitattributes` | `public/ambient/*.mp4` under LFS, as the sky loops are |
| `../../tools/make_ambient_starters.py`, `tests/test_ambient_starters.py` | new: the starter renderer and its tests |

`TrailerPlayer`, `SkyVideo`, the weather plug-in and `styles.css` are unedited. To remove the
feature, delete this folder, `public/ambient/`, those lines, and the verbs.

## Not verified

- **A real session.** The Director wire protocol and its audio are taken from fal's docs; no
  session has been opened against fal itself from this environment. Whether the audio arrives
  on the video's stream or its own, and whether it is stereo at 192 kbit/s, is handled either
  way but unseen.
- **A Titan TV.** Chrome 84's `MediaRecorder` with Opus, unmuted autoplay after a remote key,
  and 1080p VP8 playback from IndexedDB are untested on the board.
- **The loop seam.** A starter wraps every 15 s and the kept clip every 60 s, each with a
  hard cut.
- **The starters themselves** — until `tools/make_ambient_starters.py` has been run with a
  key, `public/ambient/` is empty and every scene takes the live path.
