import { useEffect, useLayoutEffect, useRef, useState, type MouseEvent, type ReactNode, type WheelEvent } from 'react';
import type { Title } from '../types/title';
import { Art } from './Art';
import { HeartIcon } from './Icons';

interface Props {
  items: Title[];
  sel: number;
  /** Ids of the titles on this profile's My List, so the row shows what is saved. */
  saved: string[];
  onPick: (index: number) => void;
  empty: ReactNode;
}

const PAD = 56; // matches .track padding
const PEEK = 80; // how much of the previous poster stays visible
const HOVER_ZONE = 0.16; // fraction of the row at each end that scrolls on hover
const HOVER_SPEED = 11; // px per frame while the pointer rests in a zone

export function PosterRow({ items, sel, saved, onPick, empty }: Props) {
  const trackRef = useRef<HTMLDivElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const [offset, setOffset] = useState(0);
  const [glide, setGlide] = useState(true); // eased only when the selection moved us
  const [drift, setDrift] = useState(0);

  /** Furthest the track can travel before its last poster is flush with the right edge. */
  const limit = () => {
    const track = trackRef.current;
    const wrap = wrapRef.current;
    return track && wrap ? Math.max(0, track.scrollWidth - wrap.clientWidth) : 0;
  };
  const pan = (to: number) => setOffset(Math.min(limit(), Math.max(0, to)));

  // Selecting a title still pulls it into view, and that move is the eased one.
  useLayoutEffect(() => {
    const track = trackRef.current;
    const poster = track?.children[sel] as HTMLElement | undefined;
    setGlide(true);
    if (!track || !poster) return setOffset(0);
    setOffset(Math.min(limit(), Math.max(0, poster.offsetLeft - PAD - (sel ? PEEK : 0))));
  }, [sel, items]);

  // Free scrolling: the row moves under a fixed selection, so browsing costs nothing.
  const onWheel = (e: WheelEvent) => {
    const delta = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
    if (!delta) return;
    setGlide(false);
    pan(offset + delta);
  };

  const onMove = (e: MouseEvent) => {
    const wrap = wrapRef.current;
    if (!wrap) return;
    const box = wrap.getBoundingClientRect(); // a ratio, so the stage's scale cancels out
    const at = (e.clientX - box.left) / box.width;
    setDrift(at < HOVER_ZONE ? -1 : at > 1 - HOVER_ZONE ? 1 : 0);
  };

  useEffect(() => {
    if (!drift) return;
    let frame = 0;
    const roll = () => {
      setGlide(false);
      setOffset((o) => Math.min(limit(), Math.max(0, o + drift * HOVER_SPEED)));
      frame = requestAnimationFrame(roll);
    };
    frame = requestAnimationFrame(roll);
    return () => cancelAnimationFrame(frame);
  }, [drift]);

  return (
    <div className="rowwrap" ref={wrapRef} onWheel={onWheel} onMouseMove={onMove} onMouseLeave={() => setDrift(0)}>
      <div
        className="track"
        ref={trackRef}
        style={{ transform: `translateX(${-offset}px)`, transition: glide ? undefined : 'none' }}
      >
        {items.length === 0
          ? empty
          : items.map((it, i) => (
              <button
                key={it.id}
                className={`poster f${i === sel ? ' sel' : ''}`}
                data-poster={i}
                aria-label={`${it.title}, ${it.typeLabel}${it.years ? ', ' + it.years : ''}${
                  saved.indexOf(it.id) >= 0 ? ', saved to My List' : ''
                }`}
                onClick={() => onPick(i)}
              >
                <Art item={it} />
                {saved.indexOf(it.id) >= 0 && (
                  <span className="savemark" aria-hidden="true"><HeartIcon /></span>
                )}
              </button>
            ))}
      </div>
    </div>
  );
}
