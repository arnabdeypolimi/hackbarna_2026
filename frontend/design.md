# Titan Browse — Design Document

A 10-foot browse screen for Titan OS televisions. React + TypeScript + Vite, built to
run on the low-powered Smart TV boards Titan OS ships on, driven entirely by a remote
control.

---

## 1. The problem in one slide

Build a streaming *browse* experience — poster row, detail panel, resume panel, search,
trailer playback — that works on hardware nobody would choose to build for:

| Target | Reality |
| --- | --- |
| Device | Titan OS TVs, 2020–2022 models |
| Browser | **Chrome 84** (July 2020) |
| Memory | **1–1.5 GB** shared with the whole TV OS |
| GPU | Weak; compositing is cheap, filters are not |
| Input | **Remote control only** — 4 arrows, OK, Back, 4 colour keys, transport keys |
| Viewing distance | ~10 feet (3 m) |
| Screen | 720p or 1080p, always 16:9 |

Every decision in this document falls out of that table. The constraint isn't "make it
pretty" — it's **make it pretty within Chrome 84 on 1 GB of RAM with no pointer**.

---

## 2. What was built

```
┌──────────────────────────────────────────────────────────────────────────┐
│                          [ Search titles or genres ]                     │
│  ┌────────────────────────────────────────────┐  ┌────────────────────┐  │
│  │  Recommended                               │  │ You watched last   │  │
│  │                                            │  │ time               │  │
│  │  ┌────┐ ┌──────┐ ┌────┐ ┌────┐ ┌────┐ ...  │  │ ┌────────────────┐ │  │
│  │  │    │ │ SEL  │ │    │ │    │ │    │      │  │ │    backdrop    │ │  │
│  │  └────┘ └──────┘ └────┘ └────┘ └────┘      │  │ └────────────────┘ │  │
│  │                                            │  │  Title             │  │
│  │  Title                   ┌──────────────┐  │  │  meta · chips      │  │
│  │  meta · chips            │   trailer    │  │  │  description       │  │
│  │  description             │   ▶ card     │  │  │  You stopped on…   │  │
│  │  [▶ Watch] [♥ Save]      └──────────────┘  │  │  [▶ Continue]      │  │
│  └────────────────────────────────────────────┘  │  [≡ Episodes]      │  │
│                                                  │  Remind me later   │  │
│         ┌───────────────────────────────────┐    └────────────────────┘  │
│         │ Popular  Top rated  New  My List ●│         ● Save to My List   │
│         └───────────────────────────────────┘                            │
└──────────────────────────────────────────────────────────────────────────┘
```

Five surfaces:

1. **Poster row** — the selected poster is physically larger than its neighbours; the
   track slides under a fixed selection point.
2. **Detail block** — metadata, genre chips, clamped synopsis, Watch / Save, and a
   trailer card.
3. **Resume panel** — "You watched last time", the browse panel's height and flat beside
   it, with continue / episodes / remind actions on its floor.
4. **Trailer player** — expands *out of the trailer card* to fill the main panel, with
   custom transport chrome over a YouTube embed.
5. **Tab bar + search** — Popular, Top rated, New releases, My List.

**Scale:** ~1,530 lines of first-party TypeScript and CSS. Two runtime dependencies:
`react` and `react-dom`. No router, no state library, no UI kit, no CSS framework.

---

## 3. Architecture

```
       index.html ──► Titan SDK (CDN, optional)  ──┐
            │                                      │
            ▼                                      ▼
        main.tsx ──► App.tsx  ◄────────── lib/titan.ts   keycodes, TTS, exit
                        │
        ┌───────────────┼────────────────┬──────────────────┐
        ▼               ▼                ▼                  ▼
   lib/csv.ts      lib/rows.ts    lib/spatialNav.ts   lib/storage.ts
   parse + map     tab → items    arrow → next el     guarded localStorage
        │               │                │                  │
        └───────────────┴────────────────┴──────────────────┘
                        │
                        ▼
              components/  (Stage, PosterRow, Detail, ResumePanel,
                            TabBar, SearchBar, TrailerPlayer,
                            ExitDialog, Toast, Art, Meta, Icons)
```

**One stateful component.** `App.tsx` owns all state — items, tab, query, selection, My
List, history, dialog, trailer. Every other component is presentational and takes props.
On a 1 GB device, a predictable single render tree beats a clever one.

**Pure logic lives in `lib/`.** Parsing, ranking and navigation are plain functions with
no React in them — they are the parts worth testing and the parts worth reusing.

**Data flows one direction:** CSV text → `toTitles()` → `Title[]` in state →
`buildRow(tab, query)` → the row → the selected `Title` → every panel.

---

## 4. Design decisions and why

Each entry is a real constraint, a real choice, and the option that was rejected.

### 4.1 A fixed 1920×1080 stage, scaled to fit

Everything is authored at exact pixel coordinates inside `#stage`, which is then scaled
with a single CSS `transform`.

```js
const s = Math.min(window.innerWidth / 1920, window.innerHeight / 1080);
```

| | |
| --- | --- |
| **Why** | TVs are always 16:9. A 720p set is a 1080p set at 0.667×. One layout, zero media queries, and the design matches the mock to the pixel on both. |
| **Bonus** | One composited transform is nearly free on the TV's GPU — cheaper than reflowing a responsive layout. |
| **Rejected** | Responsive units (`vw`, `clamp`, flex everywhere) — pays a layout tax for flexibility a TV never uses. |
| **Cost** | Coordinates must be tracked in stage units, not screen units. `TrailerPlayer` uses `offsetLeft`/`offsetTop` rather than `getBoundingClientRect()` for exactly this reason. |

### 4.2 Fake glass, not `backdrop-filter`

The whole UI is a frosted-glass language — but `backdrop-filter` is a per-frame blur of
everything behind a panel, and it will melt a TV board.

Instead: a `.room` layer of pre-blurred radial gradients sits behind the panels, and the
panels are simply semi-transparent white with a 1.5 px light edge.

```css
.panel { background: rgba(255,255,255,0.15); border: 1.5px solid rgba(255,255,255,0.3); }
```

| | |
| --- | --- |
| **Why** | The look survives; the cost doesn't. Gradients rasterise once. |
| **Rejected** | `backdrop-filter: blur()` — patchy in Chrome 84 and ruinous where it does work. |

### 4.3 Custom spatial navigation

Browsers have no notion of "the element to my left". `lib/spatialNav.ts` scores every
focusable candidate in the direction of travel:

```
score = distance_along_axis + (gap_across_axis × 2.2)
```

The 2.2 weight means **an element that lines up wins over one that is closer but off to
the side** — which is how a person reads a screen, and what a remote user expects.

Three rules layered on top:

- Inside the poster row, left/right **changes the selection** and slides the track; it
  does not move DOM focus poster-to-poster.
- Entering the row from anywhere always lands on the **selected** poster, never the
  geometrically nearest one.
- Posters scrolled outside the row's viewport are excluded as targets.

| | |
| --- | --- |
| **Rejected** | CSS `spatial-navigation` (not in Chrome 84) and `tabindex` ordering (linear; can't express a 2D layout). |

### 4.4 A generic CSS-class focus contract

Every focusable element carries the class `.f`. Navigation queries `.f`, and the
text-to-speech hook listens for `focusin` on `.f`.

| | |
| --- | --- |
| **Why** | Adding a control anywhere in the tree makes it reachable and spoken with no wiring. Navigation never needs to know the component hierarchy. |
| **Trade-off** | A convention the compiler can't enforce — forget `.f` and the control is invisible to the remote. |

### 4.5 One global key listener, always current

```js
const keyHandler = useRef(onKey);
keyHandler.current = onKey;          // refreshed every render
useEffect(() => { /* attach once, forever */ }, []);
```

| | |
| --- | --- |
| **Why** | Key handling depends on almost all state. Re-binding a `keydown` listener every render is wasteful and races; a stale closure is a bug. The ref indirection gives one listener for the app's lifetime that always calls the newest handler. |
| **Also** | Registered in the capture phase, so the app sees keys before any focused control does. |

### 4.6 CSV as the data format, with alias mapping

The app fetches a plain CSV at startup. `lib/csv.ts` contains a hand-written RFC 4180
parser (quoted fields, escaped quotes, newlines inside quotes) plus an **alias table**
mapping ~20 logical fields onto the header names real datasets actually use:

```
desc  ← description | overview | plot | synopsis | summary
image ← image_url | poster | poster_path | thumbnail | artwork
```

Only a `title` column is required. TMDB-style paths (`/abc.jpg`) are expanded to full
`image.tmdb.org` URLs; YouTube ids are extracted from any of the five common URL shapes.

| | |
| --- | --- |
| **Why** | A demo that only loads one company's schema is a demo. This one loads whatever the room has. |
| **Why not JSON** | The source dataset is a 660 MB CSV; converting it is a step, and CSV parses in one pass without building an intermediate object graph. |
| **Rejected** | A CSV library — ~50 lines of parser against a dependency the TV has to download and hold in memory. |

### 4.7 Errors written for the viewer, not the developer

```
That file has no title column. Add a column named "title" and try again.
```

Parse failures throw messages already fit to display on a television. There is no
separate error-copy layer, and no stack trace ever reaches the screen.

### 4.8 Hard caps on everything that grows

| Cap | Value | Reason |
| --- | --- | --- |
| `ROW_MAX` | 30 posters | Keeps the DOM small; nobody arrows past 30 |
| `MAX_TITLES` | 2,000 | Larger files are cut to the most popular |
| `MAX_FILE_BYTES` | 50 MB | Rejects imports that would exhaust TV memory |

On a 1 GB device memory limits are product decisions, so they live in `src/config.ts`
next to the data URL and image sizes.

### 4.9 Ranking that can't be gamed by one vote

"Top rated" does not sort by score. It first computes the **median vote count** across
the dataset, drops everything below it, *then* sorts by score.

```js
const min = votes[Math.floor(votes.length / 2)] || 0;
items.filter(x => x.votes >= min).sort((a, b) => b.score - a.score);
```

Without this, a single 10/10 vote tops the chart. Three lines, and the tab is credible.

### 4.10 Generated poster art as a first-class fallback

Every title gets a deterministic two-tone gradient derived from an FNV-1a hash of its
name, plus its title set in type sized so the longest word fits the poster.

```js
const a = hash(title) % 360;                    // stable hue per title
posterFontSize: min(40, 160 / (longest * 0.74)) // longest word fits the width
```

The gradient renders **immediately** and the real image fades over it; when the image
fails, the card *is* the design, not a broken-image icon.

| | |
| --- | --- |
| **Why it matters here** | TV network stacks are slow and TMDB images do fail. A grey box reads as a bug; a coloured title card reads as intent. |

### 4.11 The trailer player grows out of its card

Opening a trailer doesn't cut to a player. The card's position is captured as **insets
from the panel's edges**, and every inset animates to 0 over 700 ms.

```js
setGrow({ left: tile.offsetLeft, top: tile.offsetTop,
          right:  box.clientWidth  - tile.offsetLeft - tile.offsetWidth,
          bottom: box.clientHeight - tile.offsetTop  - tile.offsetHeight });
```

Because they are insets rather than a `transform`, the box is *pulled open from its
top-left corner* while the bottom-right barely moves — the player appears to grow out of
the thing you pressed. The chrome (title, transport, scrubber) fades in on a 450 ms
delay, after the box has settled.

Playback uses the YouTube IFrame API, loaded on demand, with **all native controls off**
(`controls: 0, disablekb: 1, modestbranding: 1`) and app-drawn chrome over the top —
because native YouTube controls are not remote-navigable. The scrubber claims left/right
for itself, so those keys seek ±5 s instead of moving focus.

### 4.12 The Titan SDK is optional at runtime

`index.html` loads the SDK from `https://sdk.titanos.tv/sdk/sdk.js`, which exists only on
a real television. Every call is guarded:

```js
const a11y = window.TitanSDK?.accessibility;
if (!a11y) return;
```

`exitApp()` returns `false` off-device so the UI can say *"Exit closes the app on the
TV"* instead of silently doing nothing.

| | |
| --- | --- |
| **Why** | The whole app develops and demos in a desktop browser with arrow keys and Esc. No emulator, no device in the loop. |

### 4.13 localStorage is assumed to fail

```js
try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* keep working */ }
```

Storage is missing or full on some sets. My List and watch history degrade to
session-only rather than crashing the app.

### 4.14 Back is a stack, not a button

One key unwinds state in a fixed order:

```
trailer open?   → close trailer
dialog open?    → close dialog
search active?  → clear search
not on Popular? → return to Popular
otherwise       → "Do you want to exit?"   (Titan requires this confirmation)
```

Back is `8` on Philips and Sharp, `461` on JVC, and `27` (Esc) on the desktop — all three
are handled, with one exception: while the search box has text, `8` is Backspace and
deletes a character.

---

## 5. Visual design language

**Type** — Sora (400/500/600/700) with system fallbacks. Sizes are 10-foot sizes: H1
56 px, panel headings 34–40 px, body 19–24 px, metadata 17 px. Nothing below 15 px.

**Colour** — everything is a token in `:root`:

| Token | Value | Use |
| --- | --- | --- |
| `--glass` | `rgba(255,255,255,.15)` | panel fill |
| `--glass-edge` | `rgba(255,255,255,.30)` | 1.5 px panel edge |
| `--chip` | `rgba(255,255,255,.16)` | chips, buttons |
| `--ink` / `--ink-2` / `--ink-3` | `#fff` / 78% / 60% | three-level text hierarchy |
| `--focus-bg` / `--focus-ink` | `#fff` / `#23211f` | focus inversion |

**Focus** — the most important visual state on a TV, and unmissable by design. Two
treatments: buttons **invert to solid white**; images take a **5 px white ring**. The
selected poster is also physically larger (262×334 vs 214×272), so the selection reads
from across a room, even in peripheral vision.

**Depth** — comes from the room: a moving sky, lights on it, and smoked glass over it. The
resume panel used to be tilted in perspective for the same effect, but a tilted panel never
reads as the same size as the flat browse panel beside it, so it stands flat and level now.

**Radii** — large and consistent: 48 px panels, 24–30 px cards, full pills on buttons.

**Motion** — 320 ms row glide, 700 ms player expansion, 250 ms toasts, all on
`cubic-bezier(.22,1,.36,1)`. The row's easing is *disabled* during free scroll, because
easing a continuous scroll feels like lag. All of it respects `prefers-reduced-motion`.

---

## 6. Interaction model

| Key | Action |
| --- | --- |
| Arrows | Move focus; left/right step the poster row |
| OK / Enter | Activate. On the selected poster, jumps to Watch |
| Back | Unwind (see 4.14) |
| **Red** | Save / remove from My List |
| **Yellow** | Import a CSV |
| Play / Play-Pause | Watch the selected title |

**Pointer support is a development affordance, not a product feature.** The mouse wheel
scrolls the row, hovering within 16% of either edge auto-scrolls at 11 px/frame, and a
CSV can be dropped anywhere on the window. None of it exists on the TV; all of it makes
the desktop demo usable.

---

## 7. Data pipeline

```
TMDB_movie_dataset_v11.csv          scripts/trim_tmdb.py           titles.csv
        660 MB                ──────────────────────────────►       280 KB
     ~1.2 M rows                filter → rank → enrich              600 rows
```

`scripts/trim_tmdb.py` (pandas) keeps only rows that will actually render well:

1. `status == Released`, `adult != true`
2. Must have title, overview, **poster**, **backdrop**, genres, release date
3. `vote_count >= 300` and `runtime > 0` — filters the noise
4. Release date not in the future
5. Sort by popularity, de-duplicate, take the top *N*

Then, optionally, it enriches each title with a **trailer**: 8 concurrent threads against
the TMDB `/movie/{id}/videos` endpoint, ranking candidates `trailer > teaser > clip`,
then official over fan-uploaded, then by resolution. It handles both v3 API keys (query
string) and v4 tokens (bearer header), backs off on HTTP 429, and reads the key from the
environment or a `.env` file.

**A ~2,300× reduction** is what lets the app start instantly on a TV.

---

## 8. Performance budget

| | |
| --- | --- |
| JS bundle | **164 KB** (React included), compiled to Chrome 84 |
| CSS bundle | **10 KB** |
| Dataset | 280 KB / 600 titles |
| Runtime dependencies | 2 (`react`, `react-dom`) |
| DOM nodes in the row | ≤ 30 posters |
| Per-frame GPU work | 1 stage transform, 1 track translate — no filters, no blurs |

The build targets `chrome84` for both JS and CSS, and uses `base: './'` so `dist/` can be
hosted from any folder or served straight off the TV.

**Deliberately avoided:** `backdrop-filter`, `aspect-ratio`, `inset`, `:has()`, and
anything else newer than Chrome 84 — enforced by the build target, not by discipline.

---

## 9. Accessibility

- **Text-to-speech through the Titan SDK.** When the TV's TTS setting is on, every
  focused control is read aloud from its `aria-label` or text content. The app subscribes
  to `onTTSSettingsChange`, so toggling it on the TV takes effect live.
- `aria-label`s are written as speech, not as labels: *"Casablanca, Movie, 1942"*,
  *"Seek. 1:12 of 2:30"*, *"Back 10 seconds"*.
- Dialogs use `role="dialog"` + `aria-modal`, focus the safe action on open (Stay), and
  **restore focus to wherever you were** on close.
- Focus is never invisible and never ambiguous.
- Decorative images are `alt=""`; icons are `aria-hidden`.

---

## 10. Known gaps and next steps

Stated plainly, because a demo that pretends to be finished is worse than one that
doesn't:

- **Watch, Episodes and Remind are placeholders.** They show a toast and write to local
  history — which is genuinely what feeds "You watched last time" — but there is no
  player behind them. The hook-up point is `watch()` in `App.tsx`.
- **My List keys on title text**, not on a stable id. Two films sharing a name collide.
- The README documents a `SHOW_TMDB_CREDIT` flag and a TMDB credit line; neither is in
  the code yet. TMDB's terms require the attribution before this ships with their data.
- No key is advertised on screen any more: the footer hint that named the red key was
  removed. The red, green and yellow keys are documented only in the README's remote table.
- **Fonts load from Google Fonts.** Self-hosting would remove a network round-trip from
  startup on a slow TV connection.
- No automated tests. `lib/csv.ts`, `lib/rows.ts` and `lib/spatialNav.ts` are pure
  functions and the obvious first targets.

---

## 11. The argument, in three lines

1. **The constraints came first.** Chrome 84, 1 GB of RAM and a remote control decided
   the architecture; the visual design was then built to survive them.
2. **The expensive-looking parts are cheap.** Glass without blur, depth from one
   transform, poster art from a hash — the look costs almost nothing per frame.
3. **It degrades everywhere it can.** No SDK, no storage, no images, no dataset, no
   trailer — the app keeps working and says something useful in each case.

---

## Appendix: suggested slide outline

| # | Slide | Source |
| --- | --- | --- |
| 1 | Title — *Titan Browse: a 10-foot UI on 2020 hardware* | — |
| 2 | The constraint table | §1 |
| 3 | The screen (screenshot / wireframe) | §2 |
| 4 | Architecture diagram | §3 |
| 5 | **Decision: the fixed stage** | §4.1 |
| 6 | **Decision: glass without blur** (cost before/after) | §4.2 |
| 7 | **Decision: spatial navigation** (the scoring formula, animated) | §4.3 |
| 8 | **Decision: load anyone's CSV** (alias table) | §4.6 |
| 9 | **Decision: the trailer grows out of the card** (video clip) | §4.11 |
| 10 | Visual language — type, tokens, focus | §5 |
| 11 | Remote control map | §6 |
| 12 | Data pipeline — 660 MB → 280 KB | §7 |
| 13 | Performance numbers | §8 |
| 14 | Accessibility | §9 |
| 15 | What's next / honest gaps | §10 |
| 16 | Close — the three-line argument | §11 |
