# Titan Browse

A 10-foot browse screen for Titan OS TVs: a "Recommended" poster row with details and a trailer card, a "You watched last time" panel, search, and tabs for Popular, Top rated, New releases and My List. Built with React, TypeScript and Vite, with the Titan SDK loaded from its CDN like Titan's own TypeScript example.

## Getting started

Requires Node 20.19 or newer.

```bash
npm install
npm run dev        # http://localhost:5173, use the arrow keys, Enter and Esc
```

## Add the dataset

The app reads **`public/data/titles.csv`** on startup. That folder only holds a placeholder README for now. Until the file is there, the app shows a "No titles yet" screen with a Load titles button.

To make the file from the TMDB export (`TMDB_movie_dataset_v11.csv`, about 660 MB), trim it to the most popular titles first:

```bash
npm run trim -- path/to/TMDB_movie_dataset_v11.csv public/data/titles.csv 600
```

This needs Python 3 with pandas. It keeps released, non-adult films that have a poster, backdrop, overview and genres and at least 300 votes, then takes the most popular ones.

Other CSVs work too. Only a `title` column is required, and common header names are recognised (`name`, `genre`, `overview`, `poster_url`, `vote_average`, `season`, `episode`, and so on; see `ALIAS` in `src/lib/csv.ts`). TMDB-style image paths such as `/abc.jpg` are turned into full `image.tmdb.org` URLs.

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
| Yellow | Import a CSV |
| Play / Play-Pause | Watch the selected title |

When the TV's text-to-speech setting is on, every focused control is read aloud through `TitanSDK.accessibility`.

## Project layout

```
public/data/          dataset slot (titles.csv goes here)
scripts/trim_tmdb.py  trims the TMDB export
src/config.ts         data URL, image sizes, row and file limits
src/App.tsx           state, data loading, remote-control handling
src/lib/csv.ts        CSV parser and column mapping
src/lib/rows.ts       what each tab shows; which title to resume
src/lib/maturity.ts   what a kids profile is allowed to see
src/lib/profiles.ts   profiles, user ids and their saved keys
src/lib/spatialNav.ts picks the next focus target for each arrow key
src/lib/titan.ts      keycodes, text-to-speech, exit
src/components/       Stage, PosterRow, Detail, ResumePanel, TabBar, SearchBar, Profiles, ExitDialog, Toast, Art
src/types/            Titan SDK types (sdk.d.ts from Titan's CDN) and app types
```

## Notes

- **Watch, trailer and episodes are placeholders.** They show a message and record the title in local history, which is what fills "You watched last time". Hook them up to your player.
- **The glass look avoids `backdrop-filter`.** Panels are semi-transparent over a pre-blurred background, which is much cheaper on 1–1.5 GB boards.
- **Images** come from TMDB's image CDN. When one fails to load, a generated title card is shown instead.
- **TMDB credit** is shown at the bottom of the screen. Turn it off with `SHOW_TMDB_CREDIT` in `src/config.ts` if you use other data.
- **Fonts** load Sora from Google Fonts with system fallbacks. For offline or faster startup, self-host the font files.
- `src/types/sdk.d.ts` is Titan's published type file. Refresh it from https://sdk.titanos.tv/sdk/sdk.d.ts when the SDK changes.
