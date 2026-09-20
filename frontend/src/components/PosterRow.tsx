import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type MouseEvent, type ReactNode, type WheelEvent } from 'react';
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

const PEEK = 80; // how much of the previous poster stays visible
/** How many tiles either side of the selection keep turning before the arc holds its angle.
    Two: at 4° a step the third tile would sit 12° off the panel's own 9°, and a poster title
    at 21° to the room loses its thinner strokes on a 720p set. */
const FAN_REACH = 2;
const HOVER_ZONE = 0.16; // fraction of the row at each end that scrolls on hover
const HOVER_SPEED = 11; // px per frame while the pointer rests in a zone

export function PosterRow({ items, sel, saved, onPick, empty }: Props) {
  const trackRef = useRef<HTMLDivElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const [offset, setOffset] = useState(0);
  const [glide, setGlide] = useState(true); // eased only when the selection moved us
  const [drift, setDrift] = useState(0);

  /** The track's own left padding, read from --pad-text rather than copied as a number here:
      the row's scroll maths is in stage units and a token that moved without this moving with
      it would put every poster a few pixels off its mark. Cached: it cannot change at runtime. */
  const pad = useRef(-1);
  const padLeft = () => {
    const track = trackRef.current;
    if (pad.current < 0 && track) pad.current = parseFloat(getComputedStyle(track).paddingLeft) || 0;
    return Math.max(0, pad.current);
  };

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
    // A focused child outside an overflow: hidden box still gets revealed by the browser
    // scrolling that box, and nothing here ever puts it back — the track would then be
    // displaced by however far it scrolled, with the selected poster off screen and the
    // detail panel describing a title nobody can see. The transform is the only transport.
    if (wrapRef.current) wrapRef.current.scrollLeft = 0;
    if (!track || !poster) return setOffset(0);
    setOffset(Math.min(limit(), Math.max(0, poster.offsetLeft - padLeft() - (sel ? PEEK : 0))));
  }, [sel, items]);

  /**
   * The row has to re-measure when the panel around it does. The browse panel is 1792px wide
   * until the avatar answers and 1204 after, and everything above only recomputes the track's
   * offset when the selection or the dataset changes — so a row scrolled near its end while
   * the panel was wide would stay scrolled past the narrow panel's limit, leaving a gap at
   * the right edge that nothing puts back.
   */
  useEffect(() => {
    const wrap = wrapRef.current;
    if (!wrap || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(() => {
      // No glide: this fires on every frame of the room opening, and an eased transform
      // chasing a value that moves every frame never arrives.
      setGlide(false);
      setOffset((o) => Math.min(limit(), Math.max(0, o)));
    });
    observer.observe(wrap);
    return () => observer.disconnect();
  }, []);

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
    <div
      className={`rowwrap${items.length === 0 ? ' bare' : ''}`}
      ref={wrapRef}
      onWheel={onWheel}
      onMouseMove={onMove}
      onMouseLeave={() => setDrift(0)}
    >
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
                // Signed steps from the selection and their absolute value, as unitless
                // numbers. The stylesheet turns them into an angle and a depth, so playback
                // can flatten the arc by zeroing one token instead of re-rendering the row.
                style={{
                  '--fan-steps': Math.max(-FAN_REACH, Math.min(FAN_REACH, i - sel)),
                  '--fan-away': Math.min(FAN_REACH, Math.abs(i - sel)),
                } as CSSProperties}
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
