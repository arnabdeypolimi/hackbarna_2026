import { useEffect, useRef, useState, type KeyboardEvent, type RefObject } from 'react';
import type { Rect, Title } from '../types/title';
import { clock, loadYouTube, type YTPlayer } from '../lib/youtube';
import { Back10Icon, CloseIcon, Fwd10Icon, PauseIcon, PlayIcon } from './Icons';
import { Chips } from './Meta';

const SKIP = 10; // seconds the transport buttons jump
const NUDGE = 5; // seconds left/right moves while the scrubber has focus

interface Props {
  item: Title;
  /** The tile's box, so the player can expand out of it instead of appearing. */
  from: Rect | null;
  scopeRef: RefObject<HTMLDivElement>;
  onClose: () => void;
}

/** Cinema-style popup: the video fills the surface, the chrome floats over it. */
export function TrailerPlayer({ item, from, scopeRef, onClose }: Props) {
  const frameRef = useRef<HTMLDivElement>(null);
  const api = useRef<YTPlayer | null>(null);
  const playRef = useRef<HTMLButtonElement>(null);
  const [grown, setGrown] = useState(!from);
  const [ready, setReady] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [time, setTime] = useState(0);
  const [length, setLength] = useState(0);

  // The API replaces the node it is handed, so give it one React does not own.
  useEffect(() => {
    let dead = false;
    loadYouTube().then(() => {
      const YT = window.YT;
      if (dead || !YT || !frameRef.current) return;
      const host = document.createElement('div');
      frameRef.current.appendChild(host);
      api.current = new YT.Player(host, {
        videoId: item.trailerKey,
        playerVars: {
          autoplay: 1, controls: 0, rel: 0, modestbranding: 1,
          playsinline: 1, disablekb: 1, fs: 0, iv_load_policy: 3,
        },
        events: {
          onReady: (e: { target: YTPlayer }) => { setReady(true); setLength(e.target.getDuration()); e.target.playVideo(); },
          onStateChange: (e: { data: number }) => setPlaying(e.data === YT.PlayerState.PLAYING),
        },
      }) as YTPlayer;
    });
    return () => {
      dead = true;
      try { api.current?.destroy(); } catch { /* already gone with the iframe */ }
      api.current = null;
      if (frameRef.current) frameRef.current.innerHTML = '';
    };
  }, [item.trailerKey]);

  useEffect(() => { playRef.current?.focus(); }, []);

  // One frame at the tile's size, then let CSS carry it up to fill the panel.
  useEffect(() => {
    const id = requestAnimationFrame(() => setGrown(true));
    return () => cancelAnimationFrame(id);
  }, []);

  useEffect(() => {
    const tick = setInterval(() => {
      const p = api.current;
      if (!p?.getCurrentTime) return;
      setTime(p.getCurrentTime() || 0);
      const d = p.getDuration() || 0;
      if (d) setLength(d);
    }, 250);
    return () => clearInterval(tick);
  }, []);

  const toggle = () => {
    const p = api.current;
    if (!p) return;
    if (playing) p.pauseVideo(); else p.playVideo();
  };
  const seek = (by: number) => {
    const p = api.current;
    if (!p) return;
    p.seekTo(Math.max(0, Math.min(length || Infinity, p.getCurrentTime() + by)), true);
  };
  const onScrubKey = (e: KeyboardEvent) => {
    if (e.keyCode === 37) { e.preventDefault(); seek(-NUDGE); }
    if (e.keyCode === 39) { e.preventDefault(); seek(NUDGE); }
  };

  const pct = length ? Math.min(100, (time / length) * 100) : 0;
  const facts = [item.years, item.rating, item.runtime].filter(Boolean);

  return (
    <div
      className={`player${grown ? ' grown' : ''}`}
      style={grown || !from ? undefined : { left: from.left, top: from.top, right: from.right, bottom: from.bottom }}
      role="dialog"
      aria-modal="true"
      aria-label={`Trailer for ${item.title}`}
      ref={scopeRef}
    >
      <div className="playerbox">
        <div className="playerframe" ref={frameRef} />
        <div className="playertop">
          <h4>{item.title}</h4>
          <Chips values={facts.length ? facts : item.genres} />
        </div>

        {ready && !playing && (
          <button className="bigplay f" aria-label={`Play trailer for ${item.title}`} onClick={toggle}>
            <PlayIcon />
          </button>
        )}

        <div className="playerbar">
          <div className="transport">
            <button className="ctl f" aria-label={`Back ${SKIP} seconds`} onClick={() => seek(-SKIP)}><Back10Icon /></button>
            <button className="ctl f" ref={playRef} aria-label={playing ? 'Pause' : 'Play'} onClick={toggle}>
              {playing ? <PauseIcon /> : <PlayIcon />}
            </button>
            <button className="ctl f" aria-label={`Forward ${SKIP} seconds`} onClick={() => seek(SKIP)}><Fwd10Icon /></button>
          </div>

          <button
            className="scrub f"
            aria-label={`Seek. ${clock(time)} of ${clock(length)}`}
            onKeyDown={onScrubKey}
            onClick={toggle}
          >
            <span className="scrubtrack"><span className="scrubfill" style={{ width: `${pct}%` }} /></span>
            <span className="scrubnow">{clock(time)}</span>
            <span className="scrubend">{clock(length)}</span>
          </button>

          <button className="shut f" aria-label="Close trailer" onClick={onClose}><CloseIcon /></button>
        </div>
      </div>
    </div>
  );
}
