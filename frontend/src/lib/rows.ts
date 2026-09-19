import type { Tab, Title } from '../types/title';
import { ROW_MAX } from '../config';

export const TAB_TITLES: Record<Tab, string> = {
  popular: 'Recommended',
  top: 'Top rated',
  recent: 'New releases',
  list: 'My List',
};

/** The search bar's matcher. The agent's `search_catalog` uses it too, so a spoken and a typed search agree. */
export function matches(x: Title, query: string): boolean {
  const q = query.toLowerCase();
  return x.title.toLowerCase().includes(q) || x.genres.some((g) => g.toLowerCase().includes(q));
}

export function buildRow(items: Title[], tab: Tab, query: string, myList: string[]): Title[] {
  if (query) return items.filter((x) => matches(x, query)).slice(0, ROW_MAX);
  switch (tab) {
    case 'popular':
      return (items.some((x) => x.pop) ? [...items].sort((a, b) => b.pop - a.pop) : items).slice(0, ROW_MAX);
    case 'top': {
      // Only rank titles with at least the median vote count, so one 10/10 vote doesn't top the list.
      const votes = items.map((x) => x.votes).sort((a, b) => a - b);
      const min = votes[Math.floor(votes.length / 2)] || 0;
      return items.filter((x) => x.votes >= min).sort((a, b) => b.score - a.score).slice(0, ROW_MAX);
    }
    case 'recent':
      return [...items].sort((a, b) => b.added - a.added).slice(0, ROW_MAX);
    case 'list':
      return items.filter((x) => myList.includes(x.id));
  }
}
