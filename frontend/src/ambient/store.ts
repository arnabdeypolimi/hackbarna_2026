/**
 * The kept scenes. A minute of video with sound is tens of MB, which localStorage cannot hold,
 * so the clips live in IndexedDB. One is made per scene, when the viewer first asks for it, and
 * kept: asking again plays it and costs nothing.
 *
 * Its own database, apart from the weather backdrops' (`titan-weather`): each keeps only a few
 * clips and drops its oldest to stay under its cap, and a scene must never push a backdrop out,
 * or the other way round. The shape is the weather store's; the two could share a factory.
 */

export interface Clip {
  /** The scene id. */
  key: string;
  blob: Blob;
  mime: string;
  seconds: number;
  /** Whether the recording has an audio track; a silent one is told apart from a muted one. */
  sound: boolean;
  made: number;
}

const DB_NAME = 'titan-ambient';
const STORE = 'clips';
/** One per scene, and a set's storage is small and shared; past this the oldest goes. */
const MAX_CLIPS = 4;

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

// Storage can be missing, blocked or full on a set, so nothing below throws: the scene is
// then shown once from the live stream and simply not kept.

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
