# Titan Browse

A 10-foot browse screen for Titan OS TVs: a "Recommended" poster row with details and a trailer card, a "You watched last time" panel, search, and tabs for Popular, Top rated, New releases and My List. Built with React, TypeScript and Vite, with the Titan SDK loaded from its CDN like Titan's own TypeScript example.

## Getting started

Requires Node 20.19 or newer, and Git LFS for the sky loops in `public/sky/`: a clone made
without it holds pointer files there instead of video, and the room simply stays still.

```bash
npm install
npm run dev        # http://localhost:5173, use the arrow keys, Enter and Esc
```

## The avatar panel

The right-hand panel is a live conversational avatar, so the app needs the Python
backend in this repository running alongside it:

```bash
# repository root, its own terminal
uv run uvicorn tv_avatar.app:app --reload --port 8000

# frontend/
npm run dev
```

Vite proxies `/config` and `/sessions` (including the control WebSocket) to
`localhost:8000`, which keeps the app same-origin — see `vite.config.ts`. Without
the backend the panel reads "Backend not running" and the rest of the app browses
normally. Without provider keys in the backend's `.env` it reads "Avatar
unavailable" and names what is missing. Both are fixed outside the browser, so the
panel's button re-checks rather than reloads: start the backend, press OK, and the
panel catches up in place.

Two things worth knowing:

- **The app connects on load.** Every reload, including a hot reload, opens a paid
  Anam and Cartesia session. Stop the dev server when you are not using it.
- **On a real television it needs https.** `getUserMedia` is blocked on an insecure
  origin, so a plain `http://` LAN address cannot reach the microphone at all.
  Desktop development on `localhost` is exempt from that rule.

The language chips pick the spoken language *and* the avatar who speaks it — one
avatar per language, taken from the backend's `avatars.yaml`. Changing language
opens a new session, because a session pins its voice and persona at creation. The
choice is remembered in `localStorage` for the whole television, not per profile.

Wire types come from `contracts/protocol.d.ts`, which is **generated** from
`src/tv_avatar/agent/commands.py` — import them, never hand-write them. The agent
does not emit commands yet (phase 2 / M3); `useAvatar` logs them and there is a
`TODO(M3)` marking where they will be dispatched. The HTTP types are the exception:
`BackendConfig` and `SessionInfo` in `src/lib/avatarClient.ts` *are* hand-written,
because `/config` and `POST /sessions` answer with ad-hoc dicts that the generator
never sees — so a change to either endpoint in `app.py` has to be mirrored there by
hand. The comment above `AvatarInfo` says the same thing at the point of use.

## Add the dataset

The app reads **`public/data/titles.csv`** on startup. That folder only holds a placeholder README for now. Until the file is there, the app shows a "No titles yet" screen with a Load titles button.

To make the file from the TMDB export (`TMDB_movie_dataset_v11.csv`, about 660 MB), trim it to the most popular titles first:

```bash
npm run trim -- path/to/TMDB_movie_dataset_v11.csv public/data/titles.csv 600
```

This needs Python 3 with pandas. It keeps released, non-adult films that have a poster, backdrop, overview and genres and at least 300 votes, then takes the most popular ones.

Other CSVs work too. Only a `title` column is required, and common header names are recognised (`name`, `genre`, `overview`, `poster_url`, `vote_average`, `season`, `episode`, and so on; see `ALIAS` in `src/lib/csv.ts`). TMDB-style image paths such as `/abc.jpg` are turned into full `image.tmdb.org` URLs.

My List and watch history are filed under each title's `id` column when the CSV has one (`id`, `tmdb_id`, `imdb_id`, `title_id`), and under its name and first year when it doesn't. Never the name alone: remakes share one, and the trimmed dataset has nine such pairs, from The Lion King to Wrong Turn.

To load the data from a server instead of bundling it, change `DATA_URL` in `src/config.ts`.

## Profiles

The avatar on the tab bar opens **"Who's watching?"**. Up to five profiles share the TV, each
with its own user id, My List and watch history, and each is either **Adult** or **Kids**:

| | Adult | Kids |
| --- | --- | --- |
| Catalogue | every title in the dataset | only titles cleared for children |
| My List and history | its own | its own |
| Marked as | — | a yellow **Kids** tag on the avatar, the tile and the heading |

A kids profile filters the dataset everywhere at once: every tab, search, the resume panel and
My List, so a title saved before the profile became a kids one simply stops showing up.
One adult profile always has to remain, or the full catalogue could never be reached again.

**How a title is cleared for kids** (`src/lib/maturity.ts`, decided once while parsing):

1. The age rating, when the CSV has one — `G`, `PG`, `TV-Y7`, `U` and ages up to 7 pass;
   `PG-13`, `R`, `TV-MA`, `NR` and ages above 7 do not.
2. Otherwise its genres. Horror, thriller, crime, war and western are out; family, kids and
   children are in; animation counts only when it isn't also action, because that is what
   separates Pixar from Berserk in a dataset with no ratings at all.

The TMDB export has no rating column, so rule 2 does the work there and clears 126 of 600 titles.
**Add a `certification` column and rule 1 takes over** — that is the one worth feeding real data.

Each profile is filed under a user id like `usr_7q4kx9m2tb5c`, shown at the bottom of its edit
form. Ids are random, never reused after a delete, and namespace that viewer's saved keys
(`mylist:<id>`, `history:<id>`). Profiles saved by an older build under `p1`-style ids are moved
onto user ids the first time this build starts, list and history with them.

Lists saved by a build that filed titles by name are refiled onto ids when a dataset loads. A name shared by a remake can't say which one was meant, so every title with that name takes the entry; unsave the wrong one and the two stay apart from then on.

## Theme

The room behind the glass is one of four skies, one for each season, and by default it follows
the calendar:

| Months | Sky |
| --- | --- |
| March – May | Blossom sky (spring) |
| June – August | Blue sky (summer) |
| September – November | Golden hour (autumn) |
| December – February | Ink sky (winter) |

The **green** key (G on a desktop keyboard) opens the picker. Choosing a sky pins it; **Follow the
season** hands the room back to the calendar. The choice is saved under `theme` and is shared by
every profile, like a picture setting on the set. The seasons are whole months in the northern
hemisphere rather than the equinoxes of wherever the set stands, because a television has no
location to give; a viewer south of the equator pins the sky they want.

The tab bar carries the same picker behind a small disc of the current sky, next to the profile
avatar, for a mouse, a keyboard or a remote without colour keys. Under the four skies the picker
adjusts the one on screen: **Room** (lighter, graded, darker) steps the scrim over the room,
**Glass** (clearer, standard, smokier) steps the panels, and **Sky** (moving, still) turns the loop
off. Adjustments are kept per sky and per device under `themeTune`. A notch below graded takes the
sky under the contrast it was measured for, and the picker says so rather than refusing.
Back closes the picker, as do its Done button and a click on the room outside the panel.

Each sky sets the whole palette, not just the room. The glass stays translucent and dark enough
for white ink, but it is smoked with the sky's own deepest stop rather than a neutral black, and so
are the scrim over the room, the dialogs, the toasts and the placeholder cards behind posters
(`tokensOf` in `src/lib/theme.ts`): spring's panels are a deep violet glass, autumn's a burnt
umber, winter's ink. The scrim is graded per sky against the lightest patch its loop ever shows,
so the focus ring keeps 3:1 on the room and secondary ink on the glass at least what the original
grey room measured. A little grain is dithered over the sky so it does not
band on an 8-bit panel; it is drawn once with a canvas at startup rather than shipped as an image.

### The sky moves

Each sky is also a short video loop in `public/sky/` that plays muted under the room's lights
and fades in once it is running. It is the one thing in the app that does work on every frame,
and it is allowed because that work happens in the set's hardware decoder, not on the CPU and
not in a filter. Three rules keep it honest: it pauses while a trailer plays, since a set usually
has one decoder; it is never mounted under `prefers-reduced-motion`; and if it fails to load or
to play, the still sky is simply what stays. Only the current sky's loop is ever requested, and
not before the titles have loaded, so its megabytes never come ahead of the data; a loop held
still in the picker is not fetched at all. The loops are Git LFS objects, which keeps 45 MB of
video out of the repository's history.

`npm run sky` builds the loops, and the choices behind the shipped set (which band of each clip)
are the flags on that script in `package.json`. A sky whose footage sits in `src/videos/`, named after the sky
(`Golden hour.mp4`) or its id (`golden.mp4`), is cut from it: at 3840×2160, and its last
second cross-faded into its first, so the clip loops without a cut. The 4K cut is a choice for
sharpness on large screens; a 16:9 band of a 4K clip holds about 1600×900 real pixels, so it is
an upscale at four times the bytes of 1080p, and a set has to decode 4K behind the interface.
`--footage-size 1920x1080` is the stage's own size and the cheaper cut if a set struggles. `--frame x,y,w,h` uses only
that part of each clip, for footage with a caption in it (`--frame golden=...` for one sky), and `--use ink="Snowfall.mp4"` gives
a sky a clip whatever it is called. A sky with no footage is synthesised from its own four stops
at 960×540: slow drifting bands, periodic in time so they loop the same way. The raw footage is
4K at 20 to 35 MB a clip and stays out of git like the TMDB export does; `public/sky/` is what
ships, at a few hundred kilobytes synthesised and some ten megabytes per loop cut from footage.

Either way the script solves each sky's `shade` twice, against the still gradient's first stop
and against the loop's lightest patch, and prints the value to set in `src/lib/theme.ts` when the
sky has less than the lighter of the two needs. The still is what shows until the loop plays,
under Sky: Still and under reduced motion, so it has to hold on its own. The script needs numpy
and an ffmpeg built with libx264, or `pip install imageio-ffmpeg`.

The room fills the browser window rather than the 16:9 stage, so a desktop window of any shape
shows sky edge to edge with the panels scaled and centred inside it. A television's window is the
stage, so it sees no difference.

## Build for the TV

```bash
npm run build      # outputs dist/
```

The build targets Chrome 84 (Titan OS TVs from 2020–2022) and uses relative paths, so `dist/` can be hosted from any folder. Test unpublished builds on a real set with Titan's DevView tool.

## Remote control

| Key | Action |
| --- | --- |
| Arrows | Move focus. Left and right step through the poster row. |
| OK / Enter | Activate. On a poster, jumps to Watch. |
| Back (8 on Philips and Sharp, 461 on JVC; Esc on desktop) | Clears search, returns to Popular, then asks "Do you want to exit?" |
| Red | Save or remove the selected title from My List |
| Green (G on desktop) | Open the theme picker |
| Yellow | Import a CSV |
| Play / Play-Pause | Watch the selected title |

When the TV's text-to-speech setting is on, every focused control is read aloud through `TitanSDK.accessibility`.

## Project layout

```
public/data/          dataset slot (titles.csv goes here)
public/sky/           the four sky loops the room plays
scripts/trim_tmdb.py  trims the TMDB export
scripts/render_sky.py renders public/sky/<id>.mp4 for every sky in theme.ts
src/config.ts         data URL, image sizes, row and file limits
src/App.tsx           state, data loading, remote-control handling
src/lib/csv.ts        CSV parser and column mapping
src/lib/rows.ts       what each tab shows; which title to resume
src/lib/maturity.ts   what a kids profile is allowed to see
src/lib/profiles.ts   profiles, user ids and their saved keys
src/lib/spatialNav.ts picks the next focus target for each arrow key
src/lib/theme.ts      the four skies, which season each belongs to, and the room's paint
src/lib/titan.ts      keycodes, text-to-speech, exit
src/components/       Stage, PosterRow, Detail, ResumePanel, TabBar, SearchBar, Profiles, ThemePicker, SkyVideo, ExitDialog, Toast, Art
src/types/            Titan SDK types (sdk.d.ts from Titan's CDN) and app types
```

## Notes

- **Watch, trailer and episodes are placeholders.** They show a message and record the title in local history, which is what fills "You watched last time". Hook them up to your player.
- **The glass look avoids `backdrop-filter`.** Panels are semi-transparent over a pre-blurred background, which is much cheaper on 1–1.5 GB boards.
- **Images** come from TMDB's image CDN. When one fails to load, a generated title card is shown instead.
- **TMDB credit** is shown at the bottom of the screen. Turn it off with `SHOW_TMDB_CREDIT` in `src/config.ts` if you use other data.
- **Fonts** load Sora from Google Fonts with system fallbacks. For offline or faster startup, self-host the font files.
- `src/types/sdk.d.ts` is Titan's published type file. Refresh it from https://sdk.titanos.tv/sdk/sdk.d.ts when the SDK changes.
