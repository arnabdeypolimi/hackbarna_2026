import { readJSON, writeJSON } from './storage';

/** The four skies, one for each season. */
export type ThemeId = 'blossom' | 'blue' | 'golden' | 'ink';

/** 'auto' hands the room to the calendar; anything else is a sky the viewer pinned. */
export type ThemeChoice = 'auto' | ThemeId;

/** One notch either side of a sky's grading. */
export type Step = -1 | 0 | 1;

/**
 * A viewer's adjustments to one sky, on top of its grading. `room` and `glass` step the scrim
 * and the glass one notch either way; `motion` is the moving sky. A notch below on either
 * takes the sky under its graded contrast, which the picker says out loud rather than forbids:
 * a viewer who wants more of the room is trading a little legibility for it, knowingly.
 */
export interface Tune {
  room: Step;
  glass: Step;
  motion: boolean;
}

export type Tunes = Record<ThemeId, Tune>;

export const DEFAULT_TUNE: Tune = { room: 0, glass: 0, motion: true };

/** True when an adjustment has taken the sky below the contrast it was graded for. */
export const belowGrade = (tune: Tune) => tune.room < 0 || tune.glass < 0;

export interface Theme {
  id: ThemeId;
  /** The sky, as the viewer picks it. */
  name: string;
  /** The season it belongs to. */
  season: string;
  /** First month of its season, 1–12. It holds until the next theme's month, so the seasons can't gap or overlap. */
  from: number;
  /** The four stops of the 135° sky, at 12.5 / 37.5 / 62.5 / 87.5%. */
  stops: [string, string, string, string];
  /**
   * How far the room is put out before the glass goes over it: the alpha of a scrim mixed from
   * the sky's own deepest stop.
   *
   * White ink and a white focus ring have to hold against whatever sky is behind them, and
   * a spring sky is four times lighter than a winter one. So the scrim is graded per sky
   * against the lightest thing the room ever shows: the still gradient's first stop, which is
   * the room under reduced motion, under Sky: Still, and on every start until the loop plays,
   * or the lightest patch of the loop when that is lighter. The ring holds 3:1 there under the
   * brightest light (3.09 on ink, 3.90-3.93 on the rest), and ink-3 on the glass over it 4.68:1
   * or better, which is the 4.5:1 AA needs for the 17px metadata that uses it. It was graded to
   * 4.17 — what the original grey room measured — which was under that bar on three of the four
   * skies; --ink-3 went from 0.6 to 0.66 white rather than the scrim going deeper, so the room
   * is no dimmer than it was. Nothing else writes directly on the room. scripts/render_sky.py
   * checks both whenever a loop is cut.
   */
  shade: number;
  /**
   * Strength of the grain dithered over the sky. A 2000px sweep between two stops crosses a new
   * 8-bit value every ~16px, which a television shows as banding; the grain breaks it up. Dark
   * skies band harder and carry more of it, light ones would just look noisy.
   */
  grain: number;
}

/**
 * Season order, starting in spring. The last season wraps past New Year, so winter runs
 * December to February.
 *
 * Whole months in the northern hemisphere rather than equinoxes and solstices: those need a
 * latitude, and a television has no location to give. South of the equator the picker pins
 * the sky the viewer wants.
 */
export const THEMES: Theme[] = [
  { id: 'blossom', name: 'Blossom sky', season: 'Spring', from: 3,
    stops: ['#ffeff6', '#f2c4dc', '#c9a6e8', '#7e8fd0'], shade: 0.56, grain: 0.02 },
  { id: 'blue', name: 'Blue sky', season: 'Summer', from: 6,
    stops: ['#e6f2ff', '#b3d9ff', '#80b3ff', '#6699e6'], shade: 0.56, grain: 0.02 },
  { id: 'golden', name: 'Golden hour', season: 'Autumn', from: 9,
    stops: ['#fff3d6', '#ffd9a0', '#ffae66', '#e07a52'], shade: 0.56, grain: 0.025 },
  // Read off the winter loop: its lightest and darkest patches, with two even steps between.
  { id: 'ink', name: 'Ink sky', season: 'Winter', from: 12,
    stops: ['#919397', '#686b71', '#40444b', '#171c25'], shade: 0.18, grain: 0.04 },
];

const WINTER = THEMES[THEMES.length - 1];

export const themeById = (id: ThemeId): Theme => THEMES.find((t) => t.id === id) || WINTER;

/** The sky for a date. Nothing claims January and February, so they fall through to winter. */
export function themeAt(date: Date): Theme {
  const month = date.getMonth() + 1;
  for (let i = THEMES.length - 1; i >= 0; i--) if (month >= THEMES[i].from) return THEMES[i];
  return WINTER;
}

export const resolve = (choice: ThemeChoice, now: Date): Theme =>
  choice === 'auto' ? themeAt(now) : themeById(choice);

/** The theme that takes over when this one's season runs out. */
export const themeAfter = (t: Theme): Theme => THEMES[(THEMES.indexOf(t) + 1) % THEMES.length];

const DAY = 24 * 60 * 60 * 1000;

/**
 * How long until the sky is due to change, so auto can wake then instead of polling.
 *
 * Capped at a day: a timeout past ~24.8 days overflows and fires at once, and a set's clock
 * can be put right in between. A wake that lands in the same season costs one render.
 */
export function msToNextSeason(date: Date): number {
  const next = themeAfter(themeAt(date));
  const at = new Date(date.getFullYear(), next.from - 1, 1, 0, 0, 0, 0);
  if (at.getTime() <= date.getTime()) at.setFullYear(at.getFullYear() + 1);
  // Never zero: a clock put back would otherwise wake this in a tight loop.
  return Math.min(DAY, Math.max(1000, at.getTime() - date.getTime()));
}

const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December'];

export const monthLabel = (month: number) => MONTHS[month - 1] || '';

export const skyOf = (t: Theme) =>
  `linear-gradient(135deg, ${t.stops[0]} 12.5%, ${t.stops[1]} 37.5%, ${t.stops[2]} 62.5%, ${t.stops[3]} 87.5%)`;

// ---------- colour ----------

type RGB = [number, number, number];
const BLACK: RGB = [0, 0, 0];

const rgbOf = (hex: string): RGB => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16)) as RGB;
const hexOf = (c: RGB) => '#' + c.map((v) => Math.round(v).toString(16).padStart(2, '0')).join('');
const mix = (a: RGB, b: RGB, t: number): RGB => [0, 1, 2].map((i) => a[i] + (b[i] - a[i]) * t) as RGB;
const rgba = (c: RGB, a: number) => `rgba(${c.map((v) => Math.round(v)).join(', ')}, ${Math.round(a * 1000) / 1000})`;

/** How far toward black the deepest stop goes to become the scrim, and the glass. */
const SCRIM_MIX = 0.75;
const GLASS_MIX = 0.7;
const GLASS_ALPHA = 0.6;
/** What one notch of the viewer's room and glass adjustments is worth. */
const ROOM_STEP = 0.12;
const GLASS_STEP = 0.15;

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

/**
 * Every surface that carries the sky's colour, derived from its four stops. :root holds the
 * winter set as its default, computed by these same rules, next to the tokens that never
 * change: white ink, the white focus ring, the chips and the room's lights.
 *
 * The glass stays translucent and dark enough for white ink, but it is smoked with the sky's
 * own deepest stop rather than a neutral black, and so are the scrim over the room, the
 * dialogs, the toasts and the placeholder cards: spring's panels are a deep violet glass,
 * autumn's a burnt umber, winter's ink. The mixes are deep because a scrim has to darken
 * efficiently; a mid-tone one would need to be nearly opaque before white ink held on it.
 */
export function tokensOf(t: Theme, tune: Tune = DEFAULT_TUNE): Record<string, string> {
  const [, s1, s2, s3] = t.stops.map(rgbOf);
  const scrim = mix(s3, BLACK, SCRIM_MIX);
  const solid = mix(s3, BLACK, 0.6);
  const shade = clamp(t.shade + ROOM_STEP * tune.room, 0.04, 0.92);
  return {
    '--sky': skyOf(t),
    '--grain': grainOf(t.grain),
    '--shade': rgba(scrim, shade),
    '--surround': hexOf(mix(s3, scrim, shade)),
    '--glass': rgba(mix(s3, BLACK, GLASS_MIX), GLASS_ALPHA + GLASS_STEP * tune.glass),
    '--solid': hexOf(solid),
    '--toast': rgba(solid, 0.9),
    '--scrim': rgba(scrim, 0.55),
    '--scrim-full': rgba(scrim, 0.96),
    '--focus-ink': hexOf(mix(s3, BLACK, 0.4)),
    '--card-a': hexOf(mix(s2, BLACK, 0.55)),
    '--card-b': hexOf(mix(s3, BLACK, 0.45)),
    '--card-c': hexOf(mix(mix(s2, s3, 0.5), BLACK, 0.5)),
    '--card-glow': rgba(s1, 0.45),
  };
}

const GRAIN_SIZE = 128;
/** Half the spread of the noise. 126.5 puts its standard deviation at ~52 of 255. */
const GRAIN_SPREAD = 126.5;
const grainCache: Record<string, string> = {};

/**
 * A tile of grey noise as a data URL, drawn once per strength and kept.
 *
 * Generated rather than shipped: the same tile as a PNG is 104 KB of base64, which is ten times
 * the whole stylesheet, and every theme would want its own alpha. Two uniforms averaged give the
 * triangular spread film grain has — mid-grey common, the extremes rare — so the tile stays
 * invisible as texture and only does the dithering.
 */
export function grainOf(alpha: number): string {
  const key = alpha.toFixed(3);
  const hit = grainCache[key];
  if (hit !== undefined) return hit;

  let value = 'none';
  try {
    const canvas = document.createElement('canvas');
    canvas.width = GRAIN_SIZE;
    canvas.height = GRAIN_SIZE;
    const ctx = canvas.getContext('2d');
    if (ctx) {
      const image = ctx.createImageData(GRAIN_SIZE, GRAIN_SIZE);
      const px = image.data;
      const a = Math.round(alpha * 255);
      for (let i = 0; i < px.length; i += 4) {
        px[i] = px[i + 1] = px[i + 2] = 127.5 + (Math.random() + Math.random() - 1) * GRAIN_SPREAD;
        px[i + 3] = a;
      }
      ctx.putImageData(image, 0, 0);
      value = `url("${canvas.toDataURL('image/png')}")`;
    }
  } catch (err) {
    // No canvas on this set: the room keeps its sky and loses only the dither.
    console.warn('[theme] grain disabled, canvas unavailable', err);
  }
  grainCache[key] = value;
  return value;
}

/** Repaints the sky's surfaces: one pass of custom properties, so the change costs one paint. */
export function applyTheme(t: Theme, tune: Tune = DEFAULT_TUNE): void {
  const style = document.documentElement.style;
  const tokens = tokensOf(t, tune);
  for (const name in tokens) style.setProperty(name, tokens[name]);
}

const STORE_KEY = 'theme';
const TUNE_KEY = 'themeTune';

/** The saved choice, or auto — including when storage holds something this build doesn't know. */
export function loadChoice(): ThemeChoice {
  const saved = readJSON<string>(STORE_KEY, 'auto');
  return saved === 'auto' || THEMES.some((t) => t.id === saved) ? (saved as ThemeChoice) : 'auto';
}

export const saveChoice = (choice: ThemeChoice): void => writeJSON(STORE_KEY, choice);

const isStep = (v: unknown): v is Step => v === -1 || v === 0 || v === 1;

/** Every sky's adjustments, field by field, so a value this build doesn't know falls back alone. */
export function loadTunes(): Tunes {
  const saved = readJSON<Record<string, Partial<Tune> | undefined>>(TUNE_KEY, {});
  const tunes = {} as Tunes;
  for (const t of THEMES) {
    const s = (saved && typeof saved === 'object' && saved[t.id]) || {};
    tunes[t.id] = {
      room: isStep(s.room) ? s.room : DEFAULT_TUNE.room,
      glass: isStep(s.glass) ? s.glass : DEFAULT_TUNE.glass,
      motion: typeof s.motion === 'boolean' ? s.motion : DEFAULT_TUNE.motion,
    };
  }
  return tunes;
}

export const saveTunes = (tunes: Tunes): void => writeJSON(TUNE_KEY, tunes);
