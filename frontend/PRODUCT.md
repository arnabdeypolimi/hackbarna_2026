# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Viewers in a European household, sitting about 10 feet from the television, holding a
remote control. There is no pointer and no keyboard.

One set is shared, which is the reason profiles exist: adults and children browse the
same catalogue from the same sofa and must not be shown the same thing. Up to five
profiles share a TV, each either **adult** or **kids**.

The job, in the viewer's words: *find something worth watching in the next few seconds,
or get back into what I was already watching.* Nobody sits down to operate a browse
screen. Browsing is the tax on watching, and the product's job is to keep it small.

## Product Purpose

The browse layer of a TV streaming app: the screen between turning the set on and
something playing. It has to present a catalogue, let a viewer search it, keep each
viewer's own list and history, and hand off to playback.

Success is measured on the hardware, not in a mock: a viewer gets from power-on to
playing something without waiting on the interface or losing their place in it, on a
2020–2022 TV board with 1–1.5 GB of shared memory and a weak GPU.

## Positioning

It holds a premium 10-foot feel on hardware that cannot afford one, by moving cost off
the per-frame path rather than by dropping the ambition:

- the frosted-glass look is produced with pre-rasterised gradients instead of a live
  `backdrop-filter`, which is patchy in Chrome 84 and ruinous where it works;
- layout is authored once at 1920×1080 and scaled with one composited transform, because
  televisions are always 16:9 and a 720p set is a 1080p set at 0.667×;
- spatial navigation is scored in first-party code, weighted so an element that lines up
  with the direction of travel beats one that is merely closer.

A neighbouring app can copy the layout. What it cannot copy without doing the same work
is staying fluid on the boards this one targets.

## Operating Context

| | |
| --- | --- |
| Device | Titan OS TVs, 2020–2022 models (Philips, Sharp, JVC) |
| Browser | Chrome 84 (July 2020) |
| Memory | 1–1.5 GB, shared with the whole TV OS |
| GPU | Weak. Compositing is cheap, filters are not |
| Screen | 720p or 1080p, always 16:9 |
| Distance | ~10 feet (3 m) |
| Input | Remote only: 4 arrows, OK, Back, 4 colour keys, transport keys |

Back is keycode 8 on Philips and Sharp, 461 on JVC, and Esc during desktop development.
Text-to-speech is driven by the television's own accessibility setting, through
`TitanSDK.accessibility`; the SDK is loaded from Titan's CDN and the app must work when
it is absent.

Development runs in a desktop browser (`npm run dev`, arrow keys and Esc) with CSV
drag-and-drop for test data. Builds are verified on a real set with Titan's DevView tool.

## Capabilities and Constraints

**Shipped today.** Poster row with tabs (Popular, Top rated, New releases, My List);
search across titles and genres; detail panel; "You watched last time" resume panel;
trailer playback in a YouTube embed with first-party transport chrome; up to five
profiles, each with a user id, its own My List and watch history, and an adult or kids
catalogue; CSV import by colour key or drag-and-drop; exit confirmation; toasts;
spoken focus.

**Content is a stand-in.** Titles come from a CSV today
(`public/data/titles.csv`, trimmed from the TMDB export by `scripts/trim_tmdb.py`; header
aliases in `src/lib/csv.ts`). A real catalogue source is expected to replace it. Future
work should assume richer metadata can arrive — age certificates, episode lists,
per-profile recommendations — and should keep the CSV path working as the fallback rather
than designing it away.

**Playback is planned, not present.** Watch, Episodes and Continue show a message and
record the title in local history, which is what fills the resume panel. Real player
integration is intended, so playback and episode surfaces are genuine future scope and
should be designed as real destinations rather than permanent stubs. The trailer player
is the one real playback surface that exists now.

**Kids policy.** A title is cleared for a kids profile by its age certificate when the
data has one, and by genre inference when it does not (`src/lib/maturity.ts`, decided
once at parse time). The current dataset carries no certificate column, so genres do all
the work and the catalogue is deliberately conservative. European markets mean numeric
certificates (6, 9, 12, 16, 18) are the systems a real feed will bring; `KIDS_MAX_AGE`
is 7 today. There is no PIN: the separation is content filtering and per-profile data,
not a lock.

**Multiple languages, not yet implemented.** The product ships to Europe in more than one
language. Every string is currently hardcoded English inside components, and the fixed
1920×1080 stage was laid out for English lengths — German and Dutch labels run
considerably longer. Localisation is a known, unbuilt requirement, and new copy should not
make it harder.

**Technical constraints that are not negotiable.** Chrome 84 is the floor, so no `inset`,
`aspect-ratio`, `:has()` or `backdrop-filter`. Two runtime dependencies, `react` and
`react-dom` — no router, state library, UI kit or CSS framework. Memory guards are real
limits, not arbitrary numbers: `MAX_TITLES` 2000, `ROW_MAX` 30 posters per row,
`MAX_FILE_BYTES` 50 MB. `localStorage` can be missing or full on a television, so every
access is guarded and the app keeps working without persistence. Saved keys are
`profiles`, `activeProfile`, `mylist:<user id>` and `history:<user id>`.

**Open product decisions.** Which catalogue source replaces the CSV; when real playback
lands and whether it uses the Titan SDK or HTML5 video; whether episode browsing is a
surface of its own; whether adult profiles ever get a PIN.

## Brand Commitments

None committed. "Titan Browse" is a working title, the product is the author's own, and
the current glass look is the incumbent implementation rather than a binding identity —
future visual work may keep, extend or replace it.

One real obligation stands while TMDB data is in use: its attribution is displayed on
screen (`SHOW_TMDB_CREDIT` in `src/config.ts`). Whether TMDB remains the source is part
of the open catalogue decision above.

## Evidence on Hand

- `public/data/titles.csv` — 600 real titles trimmed from the TMDB export: released,
  non-adult, with a poster, backdrop, overview, genres and at least 300 votes, plus
  YouTube trailer keys fetched from TMDB.
- `public/data/TMDB_movie_dataset_v11.csv` — the full source export (~660 MB).
- `scripts/trim_tmdb.py` — the trimming pipeline. `scripts/.env` holds a TMDB API key and
  must not be published.
- `design.md` — the existing design document: the incumbent visual system, its
  constraints and the rationale behind each decision.
- `README.md` — setup, dataset instructions, the remote-control map, and the profiles
  and kids-filter rules.
- `src/types/sdk.d.ts` — Titan's published SDK types, refreshed from
  `https://sdk.titanos.tv/sdk/sdk.d.ts`.

**Absences future work must not fabricate:** there is no catalogue API, no player
backend, no age-certificate data in the current dataset, no localisation files, no real
viewers, no telemetry or analytics, and no store listing or submission record.

## Product Principles

1. **The remote is the only input.** Every capability is reachable and announceable with
   four arrows, OK and Back. A control that the remote cannot reach does not exist.
2. **Spend nothing per frame.** The board has 1 GB and a weak GPU. Move cost to load
   time, to one composited transform, or out of the product.
3. **A viewer's data belongs to their profile, and a kids profile's limits hold
   everywhere at once** — every row, every search, the resume panel and My List — not
   just where the filtering was convenient to apply.
4. **Degrade, never fail.** A missing dataset, unavailable storage, a failed image and an
   absent Titan SDK each have a defined fallback that keeps the screen usable.
5. **Say what is real.** A placeholder states plainly that it is one instead of
   simulating depth the product does not have yet.

## Accessibility & Inclusion

No formal standard is committed yet — recorded as an open decision rather than assumed.

What the product already holds itself to: every focusable control carries the `.f` class,
which makes it both a spatial-navigation target and announceable, and focus is always
visibly marked. Type and hit areas are sized for a ~10-foot viewing distance. When the
television's text-to-speech setting is on, the focused control is read aloud through
`TitanSDK.accessibility`.
