// Pure derivations over /config. The language-to-avatar mapping lives here and
// nowhere else, and it is never hardcoded: avatars.yaml is free to change under us.
import type { AvatarInfo, BackendConfig, LanguageInfo } from './avatarClient';

/** The languages at least one avatar can actually speak, in catalog order. */
export function languageOptions(config: BackendConfig): LanguageInfo[] {
  return config.languages.filter((l) => config.avatars.some((a) => a.languages.indexOf(l.code) >= 0));
}

/**
 * Who speaks a language. Several avatars may qualify — English has both Cara and
 * Igor today — and the catalogue's own default wins, so the host a viewer meets
 * first is the one the backend considers canonical.
 */
export function avatarForLanguage(config: BackendConfig, code: string): AvatarInfo | null {
  const able = config.avatars.filter((a) => a.languages.indexOf(code) >= 0);
  return able.filter((a) => a.id === config.default_avatar)[0] ?? able[0] ?? null;
}
