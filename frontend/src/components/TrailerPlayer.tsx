import {
  forwardRef, useEffect, useImperativeHandle, useLayoutEffect, useRef, useState,
  type KeyboardEvent, type RefObject,
} from 'react';
import type { Rect, Title } from '../types/title';
import type { PlaybackReport } from '../lib/tvBridge';
import { clock, loadYouTube, type YTPlayer } from '../lib/youtube';
import { Back10Icon, CloseIcon, Fwd10Icon, PauseIcon, PlayIcon } from './Icons';
import { Chips } from './Meta';

const SKIP = 10; // seconds the transport buttons jump
const NUDGE = 5; // seconds left/right moves while the scrubber has focus

/** The transport the agent drives: the same operations the on-screen buttons perform. */
export interface TrailerPlayerHandle {
  pause(): void;
  resume(): void;
  seekTo(seconds: number): void;
  seekBy(delta: number): void;
}

interface Props {
  item: Title;
  /** The box it expands out of, so the player grows from what the viewer pressed. */
  from: Rect | null;
  /** Watch fills the whole stage; the trailer card fills only the panel it sits in. */
  full?: boolean;
  scopeRef: RefObject<HTMLDivElement>;
  onClose: () => void;
  /** Fired from the existing ticker and on play/pause, so the screen state can carry a truthful position. */
  onPlayback?: (report: PlaybackReport) => void;
}

/** Cinema-style popup: the video fills the surface, the chrome floats over it. */
export const TrailerPlayer = forwardRef<TrailerPlayerHandle, Props>(function TrailerPlayer(
  { item, from, full = false, scopeRef, onClose, onPlayback }: Props,
  ref,
) {
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
      try { api.current?.destroy(); } catch (err) { console.debug('[trailer] player already gone with the iframe', err); }
      api.current = null;
      if (frameRef.current) frameRef.current.innerHTML = '';
    };
  }, [item.trailerKey]);

  useEffect(() => { playRef.current?.focus(); }, []);

  // The big play button unmounts the moment playback starts, and a control that unmounts while
  // focused drops the remote on <body>, where spatial navigation has nothing to score and the
  // viewer is stranded. The transport's play button is the safe home. Checked after every
  // render rather than on one dependency, because the agent's resume() can start playback too.
  useLayoutEffect(() => {
    if (document.activeElement === document.body) playRef.current?.focus();
  });

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

  // Reported on state change and once a second, not on the 250 ms tick: the backend reads
  // it only at turn start, and the report re-sends the whole screen state.
  const report = useRef(onPlayback);
  report.current = onPlayback;
  const second = Math.floor(time);
  useEffect(() => {
    report.current?.({ state: playing ? 'playing' : 'paused', position_s: second });
  }, [playing, second]);

  // The player object exists before onReady but its methods are attached only then.
  const toggle = () => {
    const p = api.current;
    if (!p?.playVideo) return;
    if (playing) p.pauseVideo(); else p.playVideo();
  };
  const seekTo = (seconds: number) => {
    const p = api.current;
    if (!p?.seekTo) return;
    p.seekTo(Math.max(0, Math.min(length || Infinity, seconds)), true);
  };
  const seek = (by: number) => {
    const p = api.current;
    if (!p?.getCurrentTime) return;
    seekTo(p.getCurrentTime() + by);
  };
  // Block bodies on purpose: YouTube's command methods return the player for chaining,
  // and a handle method's return value is read as a failure reason by the ack path.
  useImperativeHandle(ref, () => ({
    pause: () => { api.current?.pauseVideo?.(); },
    resume: () => { api.current?.playVideo?.(); },
    seekTo,
    seekBy: seek,
  }));
  const onScrubKey = (e: KeyboardEvent) => {
    if (e.keyCode === 37) { e.preventDefault(); seek(-NUDGE); }
    if (e.keyCode === 39) { e.preventDefault(); seek(NUDGE); }
  };

  const pct = length ? Math.min(100, (time / length) * 100) : 0;
  const facts = [item.years, item.rating, item.runtime].filter(Boolean);
  // Full screen is reached from Watch, so the chrome says outright that this is the trailer
  // and not the film: the product has no player of its own yet.
  const chips = facts.length ? facts : item.genres;

  return (
    <div
      className={`player${full ? ' full' : ''}${grown ? ' grown' : ''}`}
      style={grown || !from ? undefined : { left: from.left, top: from.top, right: from.right, bottom: from.bottom }}
      role="dialog"
      aria-modal="true"
      aria-label={`Trailer for ${item.title}`}
      ref={scopeRef}
    >
      <div className="playerbox">
        <div className="playerframe" ref={frameRef} />
        <div className="playertop">
          <h2>{item.title}</h2>
          <Chips values={full ? ['Trailer', ...chips] : chips} />
        </div>

        {ready && !playing && (
          <button
            className="bigplay f"
            aria-label={`Play trailer for ${item.title}`}
            // Focus moves before the press takes effect, so it never has to be rescued above.
            onClick={() => { playRef.current?.focus(); toggle(); }}
          >
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
});
