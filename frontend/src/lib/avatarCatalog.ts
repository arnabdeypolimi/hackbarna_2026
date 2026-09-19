// Pure derivations over /config. The language-to-avatar mapping lives here and
// nowhere else, and it is never hardcoded: avatars.yaml is free to change under us.
import type { AvatarInfo, BackendConfig, LanguageInfo } from './avatarClient';

/** The languages at least one avatar can actually speak, in catalog order. */
export function languageOptions(config: BackendConfig): LanguageInfo[] {
  return config.languages.filter((l) => config.avatars.some((a) => a.languages.indexOf(l.code) >= 0));
}

/**
 * Who speaks a language. A specialist beats the multilingual default — Lucía's
 * Castilian voice is the entire point of having her — so the narrowest allow-list
 * covering the language wins. The catalogue's default avatar breaks ties, and takes
 * the default language outright: that is the front door, and the default is the host
 * the catalogue means a viewer to meet there.
 *
 * The rule cannot be "prefer the default", which is the obvious reading and is wrong:
 * an avatar with no `languages` key in avatars.yaml is published as speaking every
 * language, so the default would win all four and the native voices would be dead
 * entries.
 */
export function avatarForLanguage(config: BackendConfig, code: string): AvatarInfo | null {
  const able = config.avatars.filter((a) => a.languages.indexOf(code) >= 0);
  if (!able.length) return null;
  const fallback = able.filter((a) => a.id === config.default_avatar)[0];
  if (code === config.default_language && fallback) return fallback;
  const narrowest = able.reduce((a, b) => (b.languages.length < a.languages.length ? b : a));
  const tied = able.filter((a) => a.languages.length === narrowest.languages.length);
  return tied.filter((a) => a.id === config.default_avatar)[0] ?? tied[0];
}
