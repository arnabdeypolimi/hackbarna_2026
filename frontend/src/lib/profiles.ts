import type { Profile, ProfileKind, Title } from '../types/title';
import { readJSON, writeJSON } from './storage';

/** Netflix's own cap, and the most tiles that fit the picker row at 1920 px. */
export const MAX_PROFILES = 5;
export const NAME_MAX = 20;

/** Warm tones that sit with the room's greys. The first is the avatar's original colour. */
export const AVATAR_COLORS: { value: string; name: string }[] = [
  { value: '#3e3a36', name: 'Charcoal' },
  { value: '#a8503c', name: 'Clay' },
  { value: '#3f6157', name: 'Pine' },
  { value: '#5c4a72', name: 'Plum' },
  { value: '#8a6b2e', name: 'Amber' },
];

export const KIND_LABEL: Record<ProfileKind, string> = { adult: 'Adult', kids: 'Kids' };

export const listKey = (id: string) => `mylist:${id}`;
export const historyKey = (id: string) => `history:${id}`;

/** User ids read as `usr_` + 12 Crockford base32 characters: no i, l, o or u to misread on a TV. */
const USER_ID_PREFIX = 'usr_';
const ID_CHARS = '0123456789abcdefghjkmnpqrstvwxyz';
const ID_LEN = 12;

/** crypto is absent on some TV browsers, and 256 divides 32, so neither path skews the characters. */
function randomBytes(n: number): Uint8Array {
  const bytes = new Uint8Array(n);
  try {
    crypto.getRandomValues(bytes);
    return bytes;
  } catch {
    for (let i = 0; i < n; i++) bytes[i] = Math.floor(Math.random() * 256);
    return bytes;
  }
}

/** A fresh user id. 60 bits of randomness, so two profiles never land on the same one. */
export function newUserId(): string {
  const bytes = randomBytes(ID_LEN);
  let out = USER_ID_PREFIX;
  for (let i = 0; i < ID_LEN; i++) out += ID_CHARS[bytes[i] % ID_CHARS.length];
  return out;
}

const isUserId = (id: string) => id.indexOf(USER_ID_PREFIX) === 0;

/** Up to two letters for the tile: initials of two words, or the first two of one. */
export function initials(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  if (!words.length) return '?';
  const second = words.length > 1 ? words[1][0] : words[0][1] || '';
  return (words[0][0] + second).toUpperCase();
}

/** How many profiles can still reach the whole dataset. The last one of them can't be given away. */
export const adultCount = (list: Profile[]) => list.filter((p) => p.kind === 'adult').length;

function seed(): Profile[] {
  return [{ id: newUserId(), name: 'Arik', color: AVATAR_COLORS[0].value, kind: 'adult' }];
}

/** Carries one profile's list and history over to a new user id. */
function moveProfileData(from: string, to: string): void {
  const list = readJSON<string[] | null>(listKey(from), null);
  const history = readJSON<Record<string, number> | null>(historyKey(from), null);
  if (list) writeJSON(listKey(to), list);
  if (history) writeJSON(historyKey(to), history);
  dropProfileData(from);
}

/**
 * My List and history used to be filed under the title, which remakes share, so saving one
 * Lion King saved both. Moves whatever is still filed under a title onto the id of every
 * title with that name, since an old entry can't say which one was meant. Entries that name
 * nothing in this dataset stay put for a dataset that does. Returns the profiles it changed.
 */
export function refileByTitle(profileIds: string[], items: Title[]): string[] {
  const known = new Set(items.map((t) => t.id));
  const named = new Map<string, string[]>();
  items.forEach((t) => named.set(t.title, [...(named.get(t.title) || []), t.id]));
  const idsFor = (entry: string) => (known.has(entry) ? undefined : named.get(entry));

  const moved: string[] = [];
  for (const pid of profileIds) {
    let changed = false;

    const list = new Set<string>();
    for (const entry of readJSON<string[]>(listKey(pid), [])) {
      const ids = idsFor(entry);
      if (ids) changed = true;
      for (const id of ids || [entry]) list.add(id);
    }

    const stored = readJSON<Record<string, number>>(historyKey(pid), {});
    const history: Record<string, number> = {};
    for (const entry of Object.keys(stored)) {
      const ids = idsFor(entry);
      if (ids) changed = true;
      for (const id of ids || [entry]) history[id] = Math.max(history[id] || 0, stored[entry]);
    }

    if (!changed) continue;
    writeJSON(listKey(pid), Array.from(list));
    writeJSON(historyKey(pid), history);
    moved.push(pid);
  }
  return moved;
}

/** What sits in storage: a profile saved before kinds existed has no `kind` yet. */
type SavedProfile = Omit<Profile, 'kind'> & { kind?: ProfileKind };

/**
 * Reads the saved profiles.
 *
 * Two earlier shapes are upgraded in place on the way through: the app kept one unnamed
 * viewer's list under `mylist` and `history` before profiles existed, and profiles were
 * filed under short ids like `p1` before they had user ids. Each is a one-time move, so
 * a set that has been through it reads its lists straight back.
 */
export function loadProfiles(): Profile[] {
  const saved = readJSON<SavedProfile[]>('profiles', []);
  const clean = (Array.isArray(saved) ? saved : []).filter(
    (p): p is SavedProfile =>
      !!p && typeof p.id === 'string' && !!p.id && typeof p.name === 'string' && typeof p.color === 'string',
  );

  if (!clean.length) {
    const first = seed();
    const legacyList = readJSON<string[] | null>('mylist', null);
    const legacyHistory = readJSON<Record<string, number> | null>('history', null);
    if (legacyList) writeJSON(listKey(first[0].id), legacyList);
    if (legacyHistory) writeJSON(historyKey(first[0].id), legacyHistory);
    saveProfiles(first);
    return first;
  }

  const active = readJSON<string>('activeProfile', '');
  let dirty = false;
  const list = clean.slice(0, MAX_PROFILES).map((p) => {
    const kind: ProfileKind = p.kind === 'kids' ? 'kids' : 'adult';
    if (kind !== p.kind) dirty = true;
    if (isUserId(p.id)) return { ...p, kind };
    const id = newUserId();
    moveProfileData(p.id, id);
    if (p.id === active) saveActiveId(id);
    dirty = true;
    return { ...p, id, kind };
  });
  // A profile set with no adult left could never reach the full dataset again.
  if (!adultCount(list)) {
    list[0] = { ...list[0], kind: 'adult' };
    dirty = true;
  }
  if (dirty) saveProfiles(list);
  return list;
}

export function saveProfiles(list: Profile[]): void {
  writeJSON('profiles', list);
}

export function loadActiveId(list: Profile[]): string {
  const saved = readJSON<string>('activeProfile', '');
  return list.some((p) => p.id === saved) ? saved : list[0].id;
}

export function saveActiveId(id: string): void {
  writeJSON('activeProfile', id);
}

/** A blank adult profile with its own user id, carrying the first colour nobody has taken. */
export function newProfile(list: Profile[]): Profile {
  const taken = list.map((p) => p.color);
  const free = AVATAR_COLORS.find((c) => !taken.includes(c.value)) || AVATAR_COLORS[0];
  return { id: newUserId(), name: '', color: free.value, kind: 'adult' };
}

/** Drops a deleted profile's list and history. User ids aren't reused, but nothing is left behind. */
export function dropProfileData(id: string): void {
  try {
    localStorage.removeItem(listKey(id));
    localStorage.removeItem(historyKey(id));
  } catch (err) {
    // Storage unavailable: nothing was persisted to clear.
    console.warn('[profiles] could not clear profile data', id, err);
  }
}
