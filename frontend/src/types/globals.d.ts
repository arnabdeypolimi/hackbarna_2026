import type { TitanSDK } from './sdk';

// The SDK script is loaded from Titan's CDN, so it is missing in desktop browsers.
declare global {
  interface Window {
    TitanSDK?: TitanSDK;
    SmartTvA_API?: { exit?: () => void };
    // Loaded on demand from YouTube's CDN for the trailer player's own controls.
    YT?: {
      Player: new (host: HTMLElement, options: Record<string, unknown>) => unknown;
      PlayerState: { PLAYING: number; PAUSED: number; ENDED: number };
    };
    onYouTubeIframeAPIReady?: () => void;
  }
}

export {};
