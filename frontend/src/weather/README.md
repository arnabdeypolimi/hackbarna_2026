# Weather backdrops

A plug-in for the theme picker. Pick a weather next to the four seasonal skies and an agent
makes a moving backdrop for that season and weather with fal's **MiniMax H3 Max Director**,
keeps it on the TV, and plays it in the room in place of the season's own loop.

With no weather picked the plug-in renders nothing: the room is the calendar's season, or the
sky the viewer pinned, exactly as before. **None** puts it back.

## How it works

```
theme picker ── press "Rain" ──▶ WeatherThemeAgent (agent.ts)
                                   │  fal.realtime.open(wma("minimax/h3-max/director"))
                                   ▼
                     theme agent proxy  ──▶  fal (adds the key)      [src/tv_avatar/theme_agent]
                                   ▲
   browser ◀── WebRTC video, straight from fal ─┘   recorded ~40 s ──▶ IndexedDB ──▶ <video loop>
```

- **One session per season and weather, then it is kept.** Director is a live stream, billed at
  $0.08/s with a 60 s minimum (at least **$4.80 a session**). It cannot be a background that
  stays generating, so the agent records the stream once and the room plays the clip: one
  hardware-decoded `<video>`, like the season loop. Pressing a weather that is already made
  plays it and costs nothing. Up to 8 clips are kept; the oldest goes first.
- **Nothing is made without a press.** A season turning over never starts a session; the room
  falls back to that season's own sky until the viewer presses the weather again.
- **The key never reaches the browser.** The SDK's `proxyUrl` sends its three bridge calls to the
  theme agent, which adds the key, allows only those calls for one model, and caps sessions per day.
- **Legibility.** White ink is graded against the lightest sky a season can show. A generated
  picture can be lighter than winter's, so `floorOf()` adds a scrim that brings any sky up to the
  light skies' grade (0.56) under the room's own shade.

## Run it

```bash
# 1. the agent's proxy (its own server, port 8010); FAL_KEY in .env, see .env.example
uv run uvicorn tv_avatar.theme_agent.app:create_theme_agent_app --factory --port 8010

# 2. the TV app
cd frontend && npm run dev
```

`VITE_WEATHER_PROXY_URL` points the app at the proxy when it is not on `localhost:8010`. A TV
build needs it over https, and its origin in the proxy's `THEME_AGENT_ORIGINS`. Localhost is
allowed on any port, so Vite moving to 5174, 5175… when 5173 is busy needs no change; set
`THEME_AGENT_ALLOW_LOCALHOST=false` on an agent that is not on a developer's machine.
The agent reads `.env` once at start: restart it after changing a setting.

**If a press fails**, the note under the row says which of these it is (a bare "Failed to fetch"
is never shown; the agent asks the proxy's `/health` and works it out):

| Note says | Cause | Fix |
| --- | --- | --- |
| the agent is not running at … | step 1 above is not running | start it |
| the agent refused this page (origin) | the app is open at a non-localhost origin the proxy does not list, e.g. a LAN IP | open it on `localhost`, or add the origin to `THEME_AGENT_ORIGINS` and restart |
| the agent has no FAL_KEY, or its FAL_KEY has characters a key cannot contain | blank key, or stray characters around it (a quote typed on some keyboard layouts becomes `¨`) | write it bare in `.env`, no quotes, and restart; `http://localhost:8010/health` shows the `problem` |
| anything else | fal's own error, passed through | see `[weather] fal:` in the console (dev builds) |

**Without a key:** open the app with `?weatherMock=1&weatherSeconds=6`. A canvas that draws the
weather stands in for fal, and everything else — recording, storing, looping — is the real code.

## What it touches outside this folder

| File | Change |
| --- | --- |
| `src/App.tsx` | an import and one `<WeatherTheme … />`, added; nothing modified |
| `package.json` | `@fal-ai/client` pinned to `1.11.0-alpha.3`: `realtime.open` and `wma` are not in a stable release yet |
| `../../.env.example`, `../../pyproject.toml` | the proxy's blank settings; `httpx` declared as the runtime dependency it already was transitively |

`ThemePicker`, `SkyVideo`, `lib/theme.ts` and `styles.css` are unedited. The plug-in reaches the
room and the open dialog by portal. To remove it, delete this folder and those two lines.

## Not verified

- **A real session.** The Director wire protocol here is taken from fal's docs and the SDK's
  source; the proxy contract is tested against the real SDK, but no session has been opened
  against fal itself (no key in this environment).
- **A Titan TV.** Chrome 84, its WebRTC, `MediaRecorder` and IndexedDB, and whether VP8 in WebM
  plays smoothly on the board are untested. Where the browser lacks them the picker shows no
  weather row.
- **The loop seam.** The clip is a hard cut where it wraps, roughly every 40 s.
