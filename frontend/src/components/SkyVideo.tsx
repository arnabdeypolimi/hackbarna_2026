import { useEffect, useRef, useState } from 'react';
import type { Theme } from '../lib/theme';

/** Read once: a set does not change this while the app runs, and a media query per render is waste. */
const STILL = typeof window !== 'undefined' && !!window.matchMedia
  && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

export const skyUrl = (t: Theme) => `${import.meta.env.BASE_URL}sky/${t.id}.mp4`;

/**
 * The moving sky: one muted loop, decoded by the television's hardware, under the room's lights.
 *
 * It fades in over the still sky only once it is actually playing, so a slow network, a set
 * that cannot decode it, or a missing file all leave the room looking as it did before, just
 * not moving. Under prefers-reduced-motion it is not mounted at all. It pauses while a trailer
 * plays, because a set usually has one decoder and the trailer is what the viewer asked for.
 * A viewer can also turn it off per sky in the picker, which unmounts it the same way.
 */
export function SkyVideo({ theme, paused, enabled }: { theme: Theme; paused: boolean; enabled: boolean }) {
  const ref = useRef<HTMLVideoElement>(null);
  const [live, setLive] = useState(false);
  const src = skyUrl(theme);

  // A new clip starts hidden; the still sky covers its load.
  useEffect(() => { setLive(false); }, [src]);

  useEffect(() => {
    const v = ref.current;
    if (!v) return;
    const sync = () => {
      if (paused || document.hidden) v.pause();
      else { v.muted = true; v.play().catch(() => { /* the still sky stays */ }); }
    };
    sync();
    document.addEventListener('visibilitychange', sync);
    return () => document.removeEventListener('visibilitychange', sync);
  }, [paused, src]);

  if (STILL || !enabled) return null;
  return (
    <video
      ref={ref}
      className={`sky${live ? ' on' : ''}`}
      src={src}
      muted
      loop
      playsInline
      preload="auto"
      aria-hidden="true"
      onPlaying={() => setLive(true)}
      onError={() => setLive(false)}
    />
  );
}
