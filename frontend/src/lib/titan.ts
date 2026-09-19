// Titan OS remote keycodes: https://docs.titanos.tv/remote-control
export const KEY = {
  LEFT: 37, UP: 38, RIGHT: 39, DOWN: 40, ENTER: 13,
  RED: 403, GREEN: 404, YELLOW: 405, BLUE: 406,
  PLAY: 415, PLAY_PAUSE: 179,
} as const;

// Back is 8 on Philips/Sharp and 461 on JVC; Escape covers desktop testing.
export const BACK_KEYS = [8, 461, 27];

// Green opens the theme picker. G stands in on a desktop keyboard, which has no colour keys.
export const THEME_KEYS: number[] = [KEY.GREEN, 71];

let ttsEnabled = false;

export async function initTTS(): Promise<void> {
  const a11y = window.TitanSDK?.accessibility;
  if (!a11y) return;
  try {
    if (!(await a11y.isTTSSupported())) return;
    ttsEnabled = (await a11y.getTTSSettings()).enabled;
    a11y.onTTSSettingsChange((s) => { ttsEnabled = s.enabled; });
  } catch {
    ttsEnabled = false;
  }
}

export function speak(text: string): void {
  const a11y = window.TitanSDK?.accessibility;
  if (!ttsEnabled || !a11y || !text) return;
  a11y.stopSpeaking().then(() => a11y.startSpeaking(text)).catch(() => undefined);
}

/** Returns false when not on a TV, so the caller can tell the user. */
export function exitApp(): boolean {
  const api = window.SmartTvA_API;
  if (api?.exit) { api.exit(); return true; }
  window.close();
  return false;
}
