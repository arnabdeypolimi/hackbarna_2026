// Everything that knows both the app's Title and the wire protocol. Pure, so App.tsx
// never handles protocol shapes and the two translations that cross the socket — ids
// and screen state — have exactly one home.
import type { Playback, ScreenState, Tile } from '@contracts/protocol';
import type { Tab, Title } from '../types/title';
import { matches } from './rows';

// The backend's title_id is str(tmdb_id) end to end (D11) and its catalog enriches
// tiles by that key. The app prefixes numeric dataset ids with `id:` so they cannot
// be mistaken for a title (csv.ts). Strip it on the way out, accept both on the way in.
export const toWireId = (t: Title): string => (t.id.startsWith('id:') ? t.id.slice(3) : t.id);

export function fromWireId(id: string, catalog: Title[]): Title | undefined {
  return catalog.find((t) => t.id === `id:${id}` || t.id === id);
}

export interface PlaybackReport {
  state: Playback['state'];
  position_s: number;
}

export const STOPPED: PlaybackReport = { state: 'stopped', position_s: 0 };

// The prompt lists every tile it is given, so send a window, not the row: the model
// needs the neighbours of the focus to resolve "the next one", not all 500 titles.
const WINDOW = 8;

export function deriveScreenState(args: {
  tab: Tab;
  query: string;
  /** Label of the rail the agent put up with `show_titles`, when that is what the row shows. */
  agentRail: string | null;
  row: Title[];
  selIdx: number;
  playing: Title | null;
  playback: PlaybackReport;
}): ScreenState {
  const { tab, query, agentRail, row, selIdx, playing, playback } = args;
  const lo = Math.max(0, selIdx - WINDOW);
  // `position` is the absolute row index so focus_index and the prompt's [n] labels agree.
  const tiles: Tile[] = row
    .slice(lo, selIdx + WINDOW + 1)
    .map((t, i) => ({ title_id: toWireId(t), name: t.title, position: lo + i }));
  return {
    // The Detail side panel shows whatever is focused, so it is part of `grid`, not a view.
    view: playing ? 'player' : 'grid',
    rail_id: query ? `search:${query}` : agentRail ? `agent:${agentRail}` : tab,
    focus_index: row.length ? selIdx : null,
    tiles,
    playback: playing
      ? {
        // A mounted player that has not reported yet is loading, not stopped: "stopped
        // with a title" would be a contradiction the prompt cannot render.
        state: playback.state === 'stopped' ? 'paused' : playback.state,
        title_id: toWireId(playing),
        position_s: playback.position_s,
      }
      : { state: 'stopped', title_id: null, position_s: 0 },
  };
}

export function searchCatalog(catalog: Title[], query: string, limit: number): Title[] {
  return catalog
    .filter((x) => matches(x, query))
    .sort((a, b) => b.pop - a.pop)
    .slice(0, limit);
}
