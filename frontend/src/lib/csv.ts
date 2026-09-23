import type { Title } from '../types/title';
import { isKidSafe } from './maturity';
import { BACKDROP_SIZE, MAX_TITLES, POSTER_SIZE, TMDB_IMAGE_BASE } from '../config';

/**
 * RFC 4180 CSV parser: quoted fields, escaped quotes and newlines inside quotes.
 *
 * Fields are sliced out as runs, not built a character at a time: this runs on the UI
 * thread of a TV for files up to 50 MB, and per-character `+=` was the whole import time.
 */
export function parseCSV(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = '';
  let quoted = false;
  const src = text.replace(/^\uFEFF/, '');
  const n = src.length;
  let i = 0;
  let start = 0; // where the current run of plain characters began
  while (i < n) {
    const c = src.charCodeAt(i);
    if (quoted) {
      if (c === 34 /* " */) {
        field += src.slice(start, i);
        if (src.charCodeAt(i + 1) === 34) { field += '"'; i += 2; } else { quoted = false; i++; }
        start = i;
      } else i++;
    } else if (c === 34) {
      field += src.slice(start, i);
      quoted = true;
      i++;
      start = i;
    } else if (c === 44 /* , */) {
      row.push(field + src.slice(start, i));
      field = '';
      i++;
      start = i;
    } else if (c === 10 || c === 13 /* \n \r */) {
      row.push(field + src.slice(start, i));
      rows.push(row);
      row = [];
      field = '';
      i += c === 13 && src.charCodeAt(i + 1) === 10 ? 2 : 1;
      start = i;
    } else i++;
  }
  field += src.slice(start, n);
  if (field !== '' || row.length) { row.push(field); rows.push(row); }
  return rows.filter((r) => r.some((v) => v.trim() !== ''));
}

/** Accepted header names per field, lower-cased with punctuation removed. */
const ALIAS = {
  id: ['id', 'tmdbid', 'imdbid', 'titleid'],
  title: ['title', 'name', 'showtitle', 'movietitle'],
  type: ['type', 'kind', 'category', 'format', 'contenttype'],
  start: ['yearstart', 'startyear', 'year', 'releaseyear', 'releasedate', 'firstairyear', 'firstairdate', 'released'],
  end: ['yearend', 'endyear', 'lastairyear'],
  rating: ['rating', 'agerating', 'certification', 'maturity', 'contentrating'],
  runtime: ['runtime', 'duration', 'length', 'episoderuntime'],
  genres: ['genres', 'genre', 'tags'],
  desc: ['description', 'overview', 'plot', 'synopsis', 'summary'],
  image: ['imageurl', 'image', 'poster', 'posterurl', 'posterpath', 'thumbnail', 'thumbnailurl', 'artwork'],
  backdrop: ['backdrop', 'backdroppath', 'backdropurl', 'still', 'stillurl'],
  score: ['voteaverage', 'score', 'userscore', 'imdbrating', 'averagerating'],
  votes: ['votecount', 'votes', 'numvotes'],
  pop: ['popularity', 'views', 'rank'],
  trailer: ['trailerduration', 'trailerlength'],
  trailerKey: ['trailerkey', 'youtubekey', 'videokey', 'trailerurl', 'trailerid', 'youtube'],
  added: ['dateadded', 'added', 'addedon', 'createdat'],
  season: ['season', 'seasonnumber'],
  episode: ['episode', 'episodenumber'],
  position: ['position', 'stoppedat', 'timestamp', 'progress', 'resumeat'],
  last: ['lastwatched', 'watchedat', 'lastviewed'],
} as const;
type Field = keyof typeof ALIAS;

const imageUrl = (v: string, size: string) => (!v ? '' : v.startsWith('/') ? `${TMDB_IMAGE_BASE}${size}${v}` : v);
const year = (v: string) => (v.match(/\d{4}/) || [''])[0];
/** A bare YouTube id, or one pulled out of any of its usual URL shapes. */
const youtubeId = (v: string) => {
  const m = v.match(/(?:v=|youtu\.be\/|embed\/|shorts\/)([\w-]{11})/);
  return m ? m[1] : /^[\w-]{11}$/.test(v) ? v : '';
};
const num = (v: string) => parseFloat(v) || 0;
function runtime(v: string): string {
  if (!/^\d+$/.test(v)) return v;
  const m = Number(v);
  if (!m) return '';
  return m >= 60 ? `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m` : `${m}m`;
}

/** Turns CSV text into titles. Throws an Error with a message meant for the viewer. */
export function toTitles(text: string): Title[] {
  const rows = parseCSV(text);
  if (rows.length < 2) throw new Error('That file has no rows under the header. Add at least one title and try again.');

  const head = rows[0].map((h) => h.toLowerCase().replace(/[^a-z0-9]/g, ''));
  const col = {} as Record<Field, number>;
  (Object.keys(ALIAS) as Field[]).forEach((k) => {
    col[k] = head.findIndex((h) => (ALIAS[k] as readonly string[]).includes(h));
  });
  if (col.title < 0) throw new Error('That file has no title column. Add a column named "title" and try again.');
  const get = (r: string[], k: Field) => (col[k] >= 0 ? (r[col[k]] || '').trim() : '');

  // Even a file that repeats an id, or a title and year, gets a key of its own for every row.
  const seen = new Map<string, number>();
  const uniqueId = (base: string) => {
    const n = (seen.get(base) || 0) + 1;
    seen.set(base, n);
    return n > 1 ? `${base}#${n}` : base;
  };

  let items: Title[] = rows.slice(1).map((r) => {
    const typeText = get(r, 'type').toLowerCase();
    const kind: Title['kind'] =
      /movie|film/.test(typeText) ? 'movie'
      : /series|show|tv|season/.test(typeText) || get(r, 'season') ? 'series'
      : 'movie';
    const s = year(get(r, 'start'));
    const e = year(get(r, 'end'));
    const rating = get(r, 'rating');
    // Kept whole for the kids check; only the first three become chips.
    const genres = get(r, 'genres').split(/[|;,/]/).map((g) => g.trim()).filter(Boolean);
    const title = get(r, 'title');
    const own = get(r, 'id');
    return {
      // The dataset's own id when it has one, else name and first year, which tells remakes apart.
      // The `id:` prefix keeps a numeric id from reading as a title like "12" in refileByTitle.
      id: uniqueId(own ? `id:${own}` : `${title} (${s})`),
      title,
      kind,
      typeLabel: get(r, 'type') || (kind === 'series' ? 'TV Series' : 'Movie'),
      years: kind === 'series' && s ? (e ? `${s}–${e}` : `${s}–present`) : s,
      rating,
      runtime: runtime(get(r, 'runtime')),
      genres: genres.slice(0, 3),
      desc: get(r, 'desc'),
      image: imageUrl(get(r, 'image'), POSTER_SIZE),
      backdrop: imageUrl(get(r, 'backdrop'), BACKDROP_SIZE),
      trailer: get(r, 'trailer'),
      trailerKey: youtubeId(get(r, 'trailerKey')),
      score: num(get(r, 'score')),
      votes: num(get(r, 'votes')),
      pop: num(get(r, 'pop')),
      added: Date.parse(get(r, 'added')) || Date.parse(get(r, 'start')) || 0,
      season: get(r, 'season'),
      episode: get(r, 'episode'),
      position: get(r, 'position'),
      last: Date.parse(get(r, 'last')) || 0,
      kidSafe: isKidSafe(rating, genres),
    };
  }).filter((x) => x.title);

  if (!items.length) throw new Error('No rows with a title were found in that file.');
  if (items.length > MAX_TITLES) items = [...items].sort((a, b) => b.pop - a.pop).slice(0, MAX_TITLES);
  return items;
}
