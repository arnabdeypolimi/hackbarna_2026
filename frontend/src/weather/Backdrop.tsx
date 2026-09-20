import { useEffect, useRef, useState } from 'react';

/** What the backdrop shows: a kept clip, or the stream that is still being kept. */
export interface Picture {
  url?: string;
  stream?: MediaStream;
}

interface Props {
  picture: Picture;
  /** Hold the picture: a trailer is playing, or the app has not loaded its titles yet. */
  paused: boolean;
  /** The extra scrim under the room's own shade; see floorOf() in WeatherTheme.tsx. */
  floor: string;
  /** The TV could not play the kept clip. */
  onBroken: () => void;
}

/**
 * The weather, in the room: one muted looping <video> and the scrim that keeps white ink
 * readable over it. It is the same kind of layer as SkyVideo's, and behaves as one: hidden until
 * it is actually playing, so a clip the TV cannot decode leaves the room as it was, and held
 * while a trailer plays, because a set usually has one decoder and the trailer is what the viewer
 * asked for. The season's own loop is taken out while this is up (see WeatherTheme.tsx), so
 * the two are never both decoding.
 */
export function Backdrop({ picture, paused, floor, onBroken }: Props) {
  const ref = useRef<HTMLVideoElement>(null);
  // Keyed by source, as SkyVideo's is: a new picture is hidden from the render that swaps it in.
  const [playing, setPlaying] = useState('');
  const id = picture.url || 'live';

  // Set by hand, not as a prop: the same element takes the live stream and then the kept clip.
  useEffect(() => {
    const v = ref.current;
    if (!v) return;
    if (picture.stream) {
      v.removeAttribute('src');
      v.srcObject = picture.stream;
    } else if (picture.url) {
      v.srcObject = null;
      v.src = picture.url;
    }
  }, [picture.stream, picture.url]);

  useEffect(() => {
    const v = ref.current;
    if (!v) return;
    const sync = () => {
      if (paused || document.hidden) v.pause();
      else { v.muted = true; v.play().catch(() => { /* the season's sky stays */ }); }
    };
    sync();
    document.addEventListener('visibilitychange', sync);
    return () => document.removeEventListener('visibilitychange', sync);
  }, [paused, id]);

  return (
    <>
      <video
        ref={ref}
        className={`wx-video${playing === id ? ' on' : ''}`}
        muted
        loop
        playsInline
        aria-hidden="true"
        onPlaying={() => setPlaying(id)}
        onError={() => { setPlaying(''); if (picture.url) onBroken(); }}
      />
      <div className="wx-floor" style={{ background: floor }} />
    </>
  );
}
