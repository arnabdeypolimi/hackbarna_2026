import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { tokensOf, type Theme } from '../lib/theme';
import { AgentError, WeatherThemeAgent, agentSupported } from './agent';
import { Backdrop, type Picture } from './Backdrop';
import { clipKey, offers, weatherById, weathersFor, type WeatherId } from './catalogue';
import { PROXY_URL, RECORD_SECONDS } from './config';
import { listClipKeys, loadClip, loadWeather, removeClip, saveClip, saveWeather } from './store';
import { WeatherRow, type Running } from './WeatherRow';
import './weather.css';

/**
 * Weather backdrops, plugged into the room and the theme picker from outside.
 *
 * The seasonal skies are untouched. With no weather picked this renders nothing, and the room
 * is the season's sky exactly as before: on the calendar's season by default, or the one the
 * viewer pinned. Picking a weather in the theme picker makes a backdrop for the sky on screen —
 * that sky's season with that weather — and plays it in the room in place of the season's loop;
 * picking None puts the season back. Nothing is made until the viewer presses a weather, since
 * each one is a paid fal session, and a season turning over never starts one on its own.
 *
 * It reaches the two places it lives in by portal rather than by props: the backdrop goes into
 * `.room`, under the room's own lights, grain and shade, and the weather row goes into the open
 * theme dialog, after the Sky row. So ThemePicker, SkyVideo and lib/theme.ts are not edited.
 */

interface Props {
  /** The sky on screen: the season a backdrop is made for and shown over. */
  theme: Theme;
  themeOpen: boolean;
  /** A trailer is playing or the titles are loading: hold the picture, as the sky's loop is held. */
  paused: boolean;
  /** The viewer's Sky setting for this sky. Still means nothing moves, so no weather is shown. */
  motion: boolean;
}

/** Read once, as SkyVideo does: a set does not change it while the app runs. */
const STILL = typeof window !== 'undefined' && !!window.matchMedia
  && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

/**
 * Where a browser cannot make or keep a backdrop, or asks for no motion, the plug-in is not
 * mounted at all: no listeners, no storage opened, and the picker shows no weather row.
 */
const CAPABLE = !STILL && agentSupported();

export function WeatherTheme(props: Props) {
  return CAPABLE ? <Plugin {...props} /> : null;
}

/**
 * The share of the room that must be put out over a backdrop for white ink to hold on it: the
 * light skies' own grade. Their shade (0.56) is what holds ink over the lightest thing any
 * sky can show, a near-white; a generated picture can be as light as that, and so can a winter
 * one, whose own shade is only 0.18 because its sky is dark.
 */
const GRADED_SHADE = 0.56;

/**
 * The scrim to lay under the room's shade so the two together reach GRADED_SHADE, tinted the way
 * the sky's own scrim is. Zero for the light skies, which already reach it.
 */
function floorOf(theme: Theme): string {
  const extra = Math.min(0.9, Math.max(0, 1 - (1 - GRADED_SHADE) / (1 - theme.shade)));
  if (extra <= 0) return 'transparent';
  const [r = '0', g = '0', b = '0'] = tokensOf(theme)['--scrim'].match(/\d+(\.\d+)?/g) || [];
  return `rgba(${r}, ${g}, ${b}, ${Math.round(extra * 1000) / 1000})`;
}

const messageOf = (e: unknown) => (e instanceof Error && e.message ? e.message : 'Something went wrong.');

const STILL_REFUSAL = 'Sky is set to Still, so a backdrop would not show. Set Sky to Moving first: making one uses a paid session.';

function Plugin({ theme, themeOpen, paused, motion }: Props) {
  const [choice, setChoice] = useState<WeatherId | null>(loadWeather);
  // Keys of the clips kept on this TV. A clip's bytes are only read when it is on screen.
  const [made, setMade] = useState<string[]>([]);
  const [stamp, setStamp] = useState(0); // moves when a kept clip is replaced by a new take
  const [running, setRunning] = useState<Running | null>(null);
  const [live, setLive] = useState<{ key: string; stream: MediaStream } | null>(null);
  const [clip, setClip] = useState<{ key: string; url: string } | null>(null);
  const [problem, setProblem] = useState('');
  const [room, setRoom] = useState<HTMLElement | null>(null);
  const [host, setHost] = useState<HTMLElement | null>(null);
  const agent = useMemo(() => new WeatherThemeAgent(PROXY_URL, RECORD_SECONDS), []);

  // A weather the sky's season has none of (snow in summer) is not applied, and comes back on its own.
  const weather = choice && offers(theme.id, choice) ? choice : null;
  const key = weather ? clipKey(theme.id, weather) : '';
  const kept = weather !== null && made.includes(key);

  const latest = useRef({ key, paused });
  latest.current = { key, paused };

  // ---------- where it lives ----------
  useLayoutEffect(() => { setRoom(document.querySelector<HTMLElement>('.room')); }, []);

  useEffect(() => {
    let alive = true;
    listClipKeys().then((keys) => { if (alive) setMade(keys); });
    return () => { alive = false; agent.cancel(); };
  }, [agent]);

  // The row goes in while the dialog is open, before the note that closes the Adjust group.
  useLayoutEffect(() => {
    if (!themeOpen) { setHost(null); return; }
    const panel = document.querySelector('.dialog.themes .tpanel');
    const before = panel && panel.querySelector('.tnote');
    if (!panel || !before) return;
    const el = document.createElement('div');
    panel.insertBefore(el, before);
    setHost(el);
    return () => { setHost(null); el.remove(); };
  }, [themeOpen]);

  // The refusal above is true only while the sky is Still; it goes when the viewer sets it moving.
  useEffect(() => { if (motion) setProblem((p) => (p === STILL_REFUSAL ? '' : p)); }, [motion]);

  // ---------- the kept clip on screen ----------
  useEffect(() => {
    if (!kept) return;
    let stale = false;
    let url = '';
    loadClip(key).then((c) => {
      if (stale) return;
      // Gone from storage behind our back, or evicted for a newer one: it is simply not made.
      if (!c) { setMade((m) => m.filter((k) => k !== key)); return; }
      url = URL.createObjectURL(c.blob);
      setClip({ key, url });
      // The clip has taken over from the stream it was recorded from.
      setLive((l) => (l && l.key === key ? null : l));
    });
    return () => { stale = true; if (url) URL.revokeObjectURL(url); };
  }, [kept, key, stamp]);

  const url = kept && clip && clip.key === key ? clip.url : null;
  const stream = live && live.key === key ? live.stream : null;
  const picture: Picture | null = !motion || !weather ? null : url ? { url } : stream ? { stream } : null;
  const showing = picture !== null;

  // The season's loop is taken out while the weather is up. A set has one decoder, and the
  // loop would only sit paused behind this picture; the class also hides it, so the order the
  // two videos were mounted in never decides which one is seen.
  useEffect(() => {
    if (!showing) return;
    const root = document.documentElement;
    const loops = () => Array.from(document.querySelectorAll<HTMLVideoElement>('.room .sky'));
    root.classList.add('wx-live');
    loops().forEach((v) => v.pause());
    // SkyVideo plays its loop again on every visibility change and pause change.
    const hush = (e: Event) => {
      const t = e.target as Element;
      if (t.classList && t.classList.contains('sky')) (t as HTMLVideoElement).pause();
    };
    document.addEventListener('playing', hush, true);
    return () => {
      document.removeEventListener('playing', hush, true);
      root.classList.remove('wx-live');
      if (!latest.current.paused && !document.hidden) loops().forEach((v) => { v.play().catch(() => undefined); });
    };
  }, [showing]);

  // ---------- making one ----------
  const begin = (next: WeatherId) => {
    if (running) {
      setProblem(`Still creating ${weatherById(running.weather).label.toLowerCase()}. Try again when it is done.`);
      return;
    }
    // Each one is a paid session, so none is started for a sky that could not show it. A
    // backdrop already made can still be picked while the sky is Still; it just waits.
    if (!motion) {
      setProblem(STILL_REFUSAL);
      return;
    }
    const season = theme.id;
    const k = clipKey(season, next);
    const label = weatherById(next).label;
    const base = { season, weather: next, total: RECORD_SECONDS };
    setChoice(next);
    saveWeather(next);
    setProblem('');
    setLive(null);
    setRunning({ ...base, phase: 'connecting', seconds: 0 });
    agent
      .create({ season, weather: next }, {
        onProgress: (p) => setRunning({ ...base, ...p }),
        onStream: (s) => setLive({ key: k, stream: s }),
      })
      .then(async (m) => {
        const saved = await saveClip({ key: k, blob: m.blob, mime: m.mime, seconds: m.seconds, made: Date.now() });
        if (!saved) throw new AgentError('There is not enough room on this TV to keep it.');
        setMade((list) => (list.includes(k) ? list : [...list, k]));
        setStamp((n) => n + 1);
        // Nothing to hand over to when the viewer has gone to another sky meanwhile.
        if (latest.current.key !== k) setLive(null);
      })
      .catch((e) => {
        setLive(null);
        setProblem(`Couldn't create ${label.toLowerCase()}. ${messageOf(e)}`);
      })
      .finally(() => setRunning(null));
  };

  const pick = (next: WeatherId | null) => {
    setProblem('');
    if (next === null) { setChoice(null); saveWeather(null); return; }
    if (made.includes(clipKey(theme.id, next))) { setChoice(next); saveWeather(next); return; }
    begin(next);
  };

  const broken = () => {
    if (!weather) return;
    removeClip(key);
    setMade((m) => m.filter((k) => k !== key));
    setProblem(`${weatherById(weather).label} could not be played on this TV, so it was removed.`);
  };

  const floor = useMemo(() => floorOf(theme), [theme]);

  return (
    <>
      {room && picture && createPortal(<Backdrop picture={picture} paused={paused} floor={floor} onBroken={broken} />, room)}
      {host && createPortal(
        <WeatherRow
          theme={theme}
          weathers={weathersFor(theme.id)}
          choice={weather}
          made={made}
          running={running}
          problem={problem}
          motion={motion}
          onPick={pick}
          onAgain={() => { if (weather) begin(weather); }}
        />,
        host,
      )}
    </>
  );
}
