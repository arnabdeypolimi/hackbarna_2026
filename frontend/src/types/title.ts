export interface Title {
  /** Unique within a dataset and stable across reloads: what My List and watch history are filed under. Titles aren't, since remakes share them. */
  id: string;
  title: string;
  kind: 'movie' | 'series';
  typeLabel: string;
  years: string;
  rating: string;
  runtime: string;
  genres: string[];
  desc: string;
  image: string;
  backdrop: string;
  trailer: string;
  trailerKey: string;
  score: number;
  votes: number;
  pop: number;
  added: number;
  season: string;
  episode: string;
  position: string;
  last: number;
  /** Cleared for a kids profile. Decided while parsing, off the full rating and genre list. */
  kidSafe: boolean;
}

export type Tab = 'popular' | 'top' | 'recent' | 'list';

/** Insets from the panel's edges, in stage units, that the player expands out of. */
export interface Rect { left: number; top: number; right: number; bottom: number }

/** Adult profiles browse the whole dataset; kids profiles only the titles cleared for them. */
export type ProfileKind = 'adult' | 'kids';

/** A viewer. Each one keeps its own user id, My List and watch history. */
export interface Profile {
  /** The viewer's user id: unique, never reused, and what every saved key is filed under. */
  id: string;
  name: string;
  color: string;
  kind: ProfileKind;
}
