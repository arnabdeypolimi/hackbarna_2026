import { readJSON, writeJSON } from '../lib/storage';
import { WEATHERS, type WeatherId } from './catalogue';

// ---------- the viewer's pick ----------

/** Its own key, apart from the theme's: turning the weather off leaves every sky setting as it was. */
const PICK_KEY = 'weatherTheme';

/** The saved weather, or null: the season's own sky, which is also what a room with no pick shows. */
export function loadWeather(): WeatherId | null {
  const saved = readJSON<string | null>(PICK_KEY, null);
  return WEATHERS.some((w) => w.id === saved) ? (saved as WeatherId) : null;
}

export const saveWeather = (weather: WeatherId | null): void => writeJSON(PICK_KEY, weather);

// ---------- the clips ----------

/**
 * A clip is a few MB of video, which localStorage cannot hold, so the clips live in
 * IndexedDB. One is made per season and weather, on the viewer's press, and kept: pressing
 * the same weather again plays it and costs nothing.
 */
export interface Clip {
  key: string;
  blob: Blob;
  mime: string;
  seconds: number;
  made: number;
}

const DB_NAME = 'titan-weather';
const STORE = 'clips';
/** A set's storage is small and shared; past this the oldest clip goes to make room. */
const MAX_CLIPS = 8;

let opened: Promise<IDBDatabase> | null = null;

function db(): Promise<IDBDatabase> {
  if (!opened) {
    opened = new Promise<IDBDatabase>((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, 1);
      req.onupgradeneeded = () => { req.result.createObjectStore(STORE, { keyPath: 'key' }); };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    // A failed open is retried on the next call rather than remembered.
    opened.catch(() => { opened = null; });
  }
  return opened;
}

const wrap = <T>(req: IDBRequest<T>): Promise<T> =>
  new Promise((resolve, reject) => { req.onsuccess = () => resolve(req.result); req.onerror = () => reject(req.error); });

async function store(mode: IDBTransactionMode): Promise<IDBObjectStore> {
  return (await db()).transaction(STORE, mode).objectStore(STORE);
}

// Storage can be missing, blocked or full on a set, so nothing below throws: the weather
// then simply is not kept, and the room stays on the season's sky.

export async function listClipKeys(): Promise<string[]> {
  try {
    return (await wrap((await store('readonly')).getAllKeys())) as string[];
  } catch {
    return [];
  }
}

export async function loadClip(key: string): Promise<Clip | null> {
  try {
    return (await wrap<Clip | undefined>((await store('readonly')).get(key))) || null;
  } catch {
    return null;
  }
}

/** Keeps the clip, and drops the oldest ones past MAX_CLIPS. False when it could not be kept. */
export async function saveClip(clip: Clip): Promise<boolean> {
  try {
    await wrap((await store('readwrite')).put(clip));
    const s = await store('readwrite');
    const all = await wrap<Clip[]>(s.getAll());
    all.sort((a, b) => a.made - b.made).slice(0, Math.max(0, all.length - MAX_CLIPS)).forEach((c) => s.delete(c.key));
    return true;
  } catch {
    return false;
  }
}

export async function removeClip(key: string): Promise<void> {
  try {
    await wrap((await store('readwrite')).delete(key));
  } catch {
    // Already gone, or storage is unavailable.
  }
}
