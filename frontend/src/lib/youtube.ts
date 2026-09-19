/** Minimal slice of the YouTube IFrame Player API that this app drives. */
export interface YTPlayer {
  playVideo(): void;
  pauseVideo(): void;
  seekTo(seconds: number, allowSeekAhead: boolean): void;
  getCurrentTime(): number;
  getDuration(): number;
  destroy(): void;
}

let pending: Promise<void> | null = null;

/** Loads the IFrame API once and resolves when window.YT is usable. */
export function loadYouTube(): Promise<void> {
  if (pending) return pending;
  pending = new Promise((resolve) => {
    if (window.YT?.Player) return resolve();
    const earlier = window.onYouTubeIframeAPIReady;
    window.onYouTubeIframeAPIReady = () => { earlier?.(); resolve(); };
    const tag = document.createElement('script');
    tag.src = 'https://www.youtube.com/iframe_api';
    document.head.appendChild(tag);
  });
  return pending;
}

/** 25:19, or 1:05:30 once a video runs past an hour. */
export function clock(seconds: number): string {
  if (!isFinite(seconds) || seconds < 0) seconds = 0;
  const s = Math.floor(seconds % 60);
  const m = Math.floor(seconds / 60) % 60;
  const h = Math.floor(seconds / 3600);
  const mm = h ? String(m).padStart(2, '0') : String(m);
  return `${h ? `${h}:` : ''}${mm}:${String(s).padStart(2, '0')}`;
}
