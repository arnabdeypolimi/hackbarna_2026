import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { CloseIcon } from '../components/Icons';
import { BACK_KEYS, KEY } from '../lib/titan';
import { SCENES, type SceneId } from './scenes';
import type { AmbientView } from './useAmbient';
import './ambient.css';

/** How long the title and note stay up once the scene plays with sound; OK brings them back. */
const CHROME_MS = 5000;

/**
 * The scene, on the whole stage, over everything: one <video> with sound, the way Watch fills
 * the stage with a trailer. Mounted only while a scene is on, by portal into the stage so its
 * chrome is laid out in stage units like the player's, and keyed by scene so a new scene is a
 * new element whose first frame is never the old picture.
 */
export function AmbientScene({ ambient }: { ambient: AmbientView }) {
  const [stage, setStage] = useState<HTMLElement | null>(null);
  useLayoutEffect(() => { setStage(document.getElementById('stage')); }, []);
  if (!ambient.scene || !stage) return null;
  return createPortal(<Scene key={ambient.scene} scene={ambient.scene} ambient={ambient} />, stage);
}

function Scene({ scene, ambient }: { scene: SceneId; ambient: AmbientView }) {
  const { picture, running, problem } = ambient;
  const ref = useRef<HTMLVideoElement>(null);
  const shut = useRef<HTMLButtonElement>(null);
  // Which source is playing, if one is. Keyed by source, as SkyVideo's is, so a swap to another
  // source is hidden from the render that makes it.
  const [playing, setPlaying] = useState('');
  // The browser refused sound: a spoken command is not the gesture autoplay with audio needs.
  const [muted, setMuted] = useState(false);
  const [held, setHeld] = useState(false); // paused by the viewer
  const [chrome, setChrome] = useState(true);
  const label = SCENES[scene].label;
  // A file or a stream; its identity is what the element is re-pointed on.
  const media = picture ? (picture.kind === 'live' ? picture.stream : picture.url) : null;
  const source = !picture ? '' : picture.kind === 'live' ? 'live' : picture.url;
  // For the listeners, which are installed once.
  const view = useRef(ambient);
  view.current = ambient;
  const pic = useRef(picture);
  pic.current = picture;

  // Set by hand, not as a prop: the same element takes a file and a stream.
  useEffect(() => {
    const v = ref.current;
    if (!v) return;
    if (media instanceof MediaStream) { v.removeAttribute('src'); v.srcObject = media; v.loop = false; }
    else if (media) { v.srcObject = null; v.src = media; v.loop = true; }
    else { v.removeAttribute('src'); v.srcObject = null; }
  }, [media]);

  const start = useCallback(() => {
    const v = ref.current;
    if (!v || !media) return;
    v.muted = false;
    v.play().then(() => setMuted(false), () => {
      // Sound needs a key press first; muted it may go, and the note says which key.
      v.muted = true;
      setMuted(true);
      v.play().catch(() => undefined);
    });
  }, [media]);

  useEffect(() => {
    const v = ref.current;
    if (held) { v?.pause(); return; }
    start();
    const sync = () => { if (document.hidden) v?.pause(); else start(); };
    document.addEventListener('visibilitychange', sync);
    return () => document.removeEventListener('visibilitychange', sync);
  }, [start, held]);

  // The room's loops — the season's sky and a weather backdrop — are held while the scene is
  // up: a set has one decoder, and nothing behind the scene is seen. Both play themselves again
  // on every visibility change, so on the way out they are told to decide afresh — each still
  // knows whether a trailer is up under it — rather than played from here.
  useEffect(() => {
    const root = document.documentElement;
    const loops = () => Array.from(document.querySelectorAll<HTMLVideoElement>('.room video'));
    root.classList.add('am-live');
    loops().forEach((v) => v.pause());
    const hush = (e: Event) => {
      const t = e.target as HTMLVideoElement;
      if (t.closest && t.closest('.room')) t.pause();
    };
    document.addEventListener('playing', hush, true);
    return () => {
      document.removeEventListener('playing', hush, true);
      root.classList.remove('am-live');
      document.dispatchEvent(new Event('visibilitychange'));
    };
  }, []);

  // Every key, while the scene is up: nothing behind it may move. On window, in the capture
  // phase, so it runs before the app's own document listener. Back closes; OK is for sound
  // and the chrome; play/pause holds the picture. Browser and system chords are left alone.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey || e.metaKey || e.altKey || /^F\d+$/.test(e.key)) return;
      e.stopPropagation();
      e.preventDefault();
      const v = ref.current;
      if (BACK_KEYS.includes(e.keyCode)) { view.current.hide(); return; }
      if (e.keyCode === KEY.ENTER) {
        // The key press is the gesture the browser wanted.
        if (v && v.muted) { v.muted = false; setMuted(false); }
        setChrome(true);
        return;
      }
      if (e.keyCode === KEY.PLAY || e.keyCode === KEY.PLAY_PAUSE) { setHeld((h) => !h); setChrome(true); }
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, []);

  // Focus goes to the close button — the app reads its label aloud — and back where it was after.
  useEffect(() => {
    const before = document.activeElement as HTMLElement | null;
    shut.current?.focus();
    return () => { if (before && document.contains(before)) before.focus(); };
  }, []);

  // The chrome stays while there is something to say, and fades once the scene simply plays.
  // A session running behind the starter is not something to say: that is the point of it.
  const live = picture !== null && picture.kind === 'live';
  const settled = !!source && playing === source && !muted && !held && !(live && running);
  useEffect(() => {
    if (!chrome || !settled) return;
    const t = window.setTimeout(() => setChrome(false), CHROME_MS);
    return () => window.clearTimeout(t);
  }, [chrome, settled]);
  const showChrome = chrome || !settled;

  let note: string;
  if (!picture) {
    note = problem ? problem
      : running && running.phase === 'connecting' ? `Making the ${label.toLowerCase()}… connecting. It takes about a minute, then it is kept on this TV.`
      : running ? `Making it: ${running.seconds} of ${running.total} s.`
      : 'Back closes it.';
  } else if (held) note = 'Paused. Press play to carry on.';
  else if (live && running) note = `Making it: ${running.seconds} of ${running.total} s. Once kept, it loops. Back closes it.`;
  else if (picture.kind === 'kept' && !picture.sound) note = 'This scene was made without sound. Back closes it.';
  else if (muted) note = 'Press OK for sound. Back closes it.';
  else note = 'Back closes it.';

  return (
    <div className="am-scene" role="dialog" aria-modal="true" aria-label={`${label}, ambient scene`}>
      <video
        ref={ref}
        className={`am-video${source && playing === source ? ' on' : ''}`}
        playsInline
        onPlaying={() => setPlaying(source)}
        onError={() => {
          setPlaying('');
          const p = pic.current;
          if (p && p.kind === 'kept') view.current.broken();
          else if (p && p.kind === 'starter') view.current.starterFailed();
        }}
      />
      <div className={`am-veil${showChrome ? '' : ' away'}`} />
      <div className={`am-chrome${showChrome ? '' : ' away'}`}>
        <h4>{label}</h4>
        <p role="status" aria-live="polite">{note}</p>
      </div>
      <button
        ref={shut}
        className={`shut f am-shut${showChrome ? '' : ' away'}`}
        aria-label={`Close the ${label.toLowerCase()}`}
        onClick={() => view.current.hide()}
      >
        <CloseIcon />
      </button>
    </div>
  );
}
