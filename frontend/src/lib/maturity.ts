import type { Profile, Title } from '../types/title';

/**
 * What a kids profile is allowed to see.
 *
 * A certificate is the best signal, so it wins when the dataset has one. TMDB's export has
 * no certificate column at all, so genres are the fallback: one adult genre disqualifies a
 * title outright, and the rest have to earn their place.
 */

/** The oldest audience a kids profile may be shown, for datasets that certify by age. */
const KIDS_MAX_AGE = 7;

/** Certificates cleared for kids, lower-cased with punctuation dropped: "TV-Y7" -> "tvy7". */
const KID_RATINGS = new Set(['g', 'pg', 'u', 'uc', 'tvy', 'tvy7', 'tvy7fv', 'tvg', 'tvpg', 'e', 'ec', 'al', 'all', 'exempt']);

/** Certificates that keep a title out however kid-friendly its genres look. */
const ADULT_RATINGS = new Set([
  'pg13', 'r', 'nc17', 'x', 'xxx', 'ao', 'm', 'ma', 'ma15', 'r18', 'tv14', 'tvma', '18plus', 'unrated', 'nr',
]);

/** Genres that say "for children" outright. */
const CHILD_GENRES = ['family', 'kids', 'children'];
const ADULT_GENRES = ['horror', 'thriller', 'crime', 'war', 'western', 'erotic'];

const normalize = (v: string) => v.toLowerCase().replace(/[^a-z0-9]/g, '');

/**
 * True when a title may appear in a kids profile.
 *
 * Takes the raw certificate and the *whole* genre list, because a title only keeps three
 * genres for its chips and the fourth is often the one that rules it out.
 */
export function isKidSafe(certificate: string, allGenres: string[]): boolean {
  const rating = normalize(certificate);
  if (rating) {
    if (KID_RATINGS.has(rating)) return true;
    if (ADULT_RATINGS.has(rating)) return false;
    // European datasets certify by age: "6", "12", "16".
    if (/^\d+$/.test(rating)) return Number(rating) <= KIDS_MAX_AGE;
    // Anything else is an unknown label, so fall through to the genres.
  }
  const genres = allGenres.map(normalize);
  const tagged = (words: string[]) => genres.some((g) => words.some((w) => g.includes(w)));
  if (tagged(ADULT_GENRES)) return false;
  if (tagged(CHILD_GENRES)) return true;
  // Animation on its own proves nothing — Berserk and Mortal Kombat Legends are cartoons too, and
  // this dataset gives them no certificate. Pairing it with Action is what separates those from
  // Pixar, at the price of keeping a few tame ones like the Spider-Verse films out.
  return tagged(['animation']) && !tagged(['action']);
}

/** The slice of the dataset one profile may browse. Every row and search reads this, not `items`. */
export function catalogFor(items: Title[], profile: Profile | undefined): Title[] {
  return profile?.kind === 'kids' ? items.filter((x) => x.kidSafe) : items;
}
