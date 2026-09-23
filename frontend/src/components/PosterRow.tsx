import {
  useEffect, useLayoutEffect, useMemo, useRef, useState,
  type CSSProperties, type MouseEvent, type ReactNode, type WheelEvent,
} from 'react';
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
const fanned = (i: number, sel: number) => i !== sel && Math.abs(i - sel) <= FAN_REACH;
const HOVER_ZONE = 0.16; // fraction of the row at each end that scrolls on hover
const HOVER_SPEED = 11; // px per frame while the pointer rests in a zone

export function PosterRow({ items, sel, saved, onPick, empty }: Props) {
  const trackRef = useRef<HTMLDivElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const [offset, setOffset] = useState(0);
  const [glide, setGlide] = useState(true); // eased only when the selection moved us
  const [drift, setDrift] = useState(0);
  const savedIds = useMemo(() => new Set(saved), [saved]);

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

  /** Where the track sits so that poster `n` is at the selection point, clamped to the row. */
  const placement = (n: number) => {
    const poster = trackRef.current?.children[n] as HTMLElement | undefined;
    if (!poster) return 0;
    return Math.min(limit(), Math.max(0, poster.offsetLeft - padLeft() - (n ? PEEK : 0)));
  };
  // The selection, readable from the resize callback below without rebuilding the observer.
  const selRef = useRef(sel);
  selRef.current = sel;

  // Selecting a title still pulls it into view, and that move is the eased one.
  useLayoutEffect(() => {
    setGlide(true);
    // A focused child outside an overflow: hidden box still gets revealed by the browser
    // scrolling that box, and nothing here ever puts it back — the track would then be
    // displaced by however far it scrolled, with the selected poster off screen and the
    // detail panel describing a title nobody can see. The transform is the only transport.
    if (wrapRef.current) wrapRef.current.scrollLeft = 0;
    setOffset(placement(sel));
  }, [sel, items]);

  /**
   * The row has to re-place itself when the panel around it changes width. The browse panel
   * is 1792px wide until the avatar answers and 1204 after, and the effect above only runs
   * when the selection or the dataset changes — so a selection made near the row's end while
   * the panel was wide stayed where the wide panel had put it, and the narrow panel clipped
   * it entirely while the detail block went on describing it.
   *
   * It re-derives the placement rather than clamping the old offset: limit() *grows* as the
   * panel narrows (the track is the same width, the window onto it smaller), so a clamp to
   * it is a no-op in the one direction this app ever resizes.
   */
  useEffect(() => {
    const wrap = wrapRef.current;
    if (!wrap || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(() => {
      const next = placement(selRef.current);
      setOffset((o) => {
        if (o === next) return o;
        // No glide, and only when there is somewhere to go: this fires on every frame of the
        // room opening, and an eased transform chasing a value that moves every frame never
        // arrives — but cancelling the glide on frames where nothing moved would turn an
        // arrow press made during the opening into a snap.
        setGlide(false);
        return next;
      });
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
                className={`poster f${i === sel ? ' sel' : ''}${fanned(i, sel) ? ' fan' : ''}`}
                data-poster={i}
                // Signed steps from the selection and their absolute value, as unitless
                // numbers, on the tiles in the arc only. The stylesheet turns them into an
                // angle and a depth, so playback can flatten the arc by zeroing one token
                // instead of re-rendering the row; tiles outside the arc get no transform at
                // all, which is what keeps a thirty-tile row at five composited layers.
                style={fanned(i, sel) ? {
                  '--fan-steps': i - sel,
                  '--fan-away': Math.abs(i - sel),
                } as CSSProperties : undefined}
                aria-label={`${it.title}, ${it.typeLabel}${it.years ? ', ' + it.years : ''}${
                  savedIds.has(it.id) ? ', saved to My List' : ''
                }`}
                onClick={() => onPick(i)}
              >
                <Art item={it} />
                {savedIds.has(it.id) && (
                  <span className="savemark" aria-hidden="true"><HeartIcon /></span>
                )}
              </button>
            ))}
      </div>
    </div>
  );
}
