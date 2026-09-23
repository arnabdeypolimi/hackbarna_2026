import type { Tab, Title } from '../types/title';
import { ROW_MAX } from '../config';

export const TAB_TITLES: Record<Tab, string> = {
  popular: 'Recommended',
  top: 'Top rated',
  recent: 'New releases',
  list: 'My List',
};

/**
 * The search bar's matcher. The agent's `search_catalog` uses it too, so a spoken and a
 * typed search agree. Every word must appear somewhere in the title or genres, but not as
 * one phrase: speech arrives without punctuation, so "2001 space odyssey" has to find
 * "2001: A Space Odyssey".
 */
export function matches(x: Title, query: string): boolean {
  const hay = `${x.title} ${x.genres.join(' ')}`.toLowerCase();
  const words = query.toLowerCase().split(/\s+/).filter((w) => w && !FILLER.has(w));
  return words.length > 0 && words.every((w) => hay.includes(w));
}

// Words a spoken request carries that no title or genre does: "space movie" must still
// find 2001: A Space Odyssey. A query made only of these matches nothing.
const FILLER = new Set([
  'a', 'an', 'the', 'some', 'any', 'me', 'for', 'of', 'to', 'in', 'on', 'with', 'about', 'like',
  'movie', 'movies', 'film', 'films', 'show', 'shows', 'series', 'title', 'titles', 'something',
]);

/**
 * The tab's ranking of the whole catalogue, before search or list filtering. Split from
 * `buildRow` so the caller can memoise it on `[items, tab]` alone: saving a title used to
 * re-sort 2000 items, even though only My List reads the saved ids.
 */
export function rankForTab(items: Title[], tab: Tab): Title[] {
  switch (tab) {
    case 'popular':
      return items.some((x) => x.pop) ? [...items].sort((a, b) => b.pop - a.pop) : items;
    case 'top': {
      // Only rank titles with at least the median vote count, so one 10/10 vote doesn't top the list.
      const votes = items.map((x) => x.votes).sort((a, b) => a - b);
      const min = votes[Math.floor(votes.length / 2)] || 0;
      return items.filter((x) => x.votes >= min).sort((a, b) => b.score - a.score);
    }
    case 'recent':
      return [...items].sort((a, b) => b.added - a.added);
    case 'list':
      return items;
  }
}

/** `ranked` is `rankForTab(items, tab)`; `items` is the unranked catalogue the search runs over. */
export function buildRow(items: Title[], ranked: Title[], tab: Tab, query: string, myList: string[]): Title[] {
  if (query) return items.filter((x) => matches(x, query)).slice(0, ROW_MAX);
  if (tab === 'list') {
    const saved = new Set(myList);
    return ranked.filter((x) => saved.has(x.id));
  }
  return ranked.slice(0, ROW_MAX);
}
