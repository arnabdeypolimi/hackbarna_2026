import { useEffect, useRef, useState } from 'react';
import type { Theme } from '../lib/theme';

/** Read once: a set does not change this while the app runs, and a media query per render is waste. */
const STILL = typeof window !== 'undefined' && !!window.matchMedia
  && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

export const skyUrl = (t: Theme) => `${import.meta.env.BASE_URL}sky/${t.id}.mp4`;

interface Props {
  theme: Theme;
  /** Hold the loop: a trailer is playing, or the app has not loaded its titles yet. */
  paused: boolean;
  /** The viewer's Sky setting for this theme; off takes the loop out altogether. */
  enabled: boolean;
}

/**
 * The moving sky: one muted loop, decoded by the television's hardware, under the room's lights.
 *
 * It fades in over the still sky only once it is actually playing, so a slow network, a set
 * that cannot decode it, or a missing file all leave the room looking as it did before, just
 * not moving. Under prefers-reduced-motion it is not mounted at all, and a viewer can turn it
 * off per sky in the picker, which unmounts it the same way. It pauses while a trailer plays,
 * because a set usually has one decoder and the trailer is what the viewer asked for.
 */
export function SkyVideo({ theme, paused, enabled }: Props) {
  if (STILL || !enabled) return null;
  // Everything the loop knows lives in Loop, so turning the sky off and on again starts it
  // over: a fresh element, hidden until it plays, and a play call on mount. With the state and
  // the effects up here they outlived the element, and the second mount came back already
  // marked live, at full opacity, on a first frame that was never played.
  return <Loop theme={theme} paused={paused} />;
}

function Loop({ theme, paused }: { theme: Theme; paused: boolean }) {
  const ref = useRef<HTMLVideoElement>(null);
  // Which clip is playing, if one is. Keyed by URL rather than a flag, so a new sky's clip is
  // hidden from the render that swaps it in: the still sky covers its load, no effect needed.
  const [playing, setPlaying] = useState('');
  const src = skyUrl(theme);

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

  // preload="none": nothing is fetched until play() asks for it, so the loop's megabytes queue
  // behind the titles and posters on a set's network rather than ahead of them, and a loop
  // held paused is never fetched at all. Only the current sky's clip is ever requested.
  return (
    <video
      ref={ref}
      className={`sky${playing === src ? ' on' : ''}`}
      src={src}
      muted
      loop
      playsInline
      preload="none"
      aria-hidden="true"
      onPlaying={() => setPlaying(src)}
      onError={() => setPlaying('')}
    />
  );
}
