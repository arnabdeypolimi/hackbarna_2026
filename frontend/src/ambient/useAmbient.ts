import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ScreenState } from '@contracts/protocol';
import type { CommandHandler } from '../hooks/useTvControl';
import { AgentError, AmbientSceneAgent, CANCELLED, agentSupported, type Progress } from './agent';
import { AUTO_SCENE, PROXY_URL, RECORD_SECONDS, STARTERS, starterUrl } from './config';
import { SCENES, isScene, type SceneId } from './scenes';
import { listClipKeys, loadClip, removeClip, saveClip } from './store';

/**
 * Ambient scenes, plugged into the TV from outside.
 *
 * The voice agent's `show_ambient` lands here (through withAmbient, below) and the scene goes on
 * full screen at once. What plays is the best the TV has: the clip it kept from an earlier
 * request, else the starter that ships with the app — while, behind it, fal Director makes the
 * full minute and keeps it for the next request. The viewer never waits on a session. Only with
 * no starter to stand in (the file missing, or `?ambientStarter=0`) is the live stream shown as
 * it arrives, with the wait for it. `hide_ambient`, Back on the remote, or a spoken "close it"
 * take the scene down. Nothing is made without the viewer asking, since each scene is a paid
 * session; a scene with a kept clip costs nothing more.
 */

/** The scene being made: which, and how far along. */
export interface Running extends Progress {
  scene: SceneId;
}

/** What is on screen for the scene. Null is a black screen and the note saying why. */
export type Picture =
  | { kind: 'kept'; url: string; sound: boolean }
  | { kind: 'starter'; url: string }
  | { kind: 'live'; stream: MediaStream };

export interface AmbientView {
  /** The scene on screen, or null: nothing is mounted. */
  scene: SceneId | null;
  picture: Picture | null;
  /** The session making this scene's clip, and how far along; null when none is. */
  running: Running | null;
  /** Why the last attempt to make this scene failed. Shown only when there is nothing to show instead. */
  problem: string;
  /** Puts the scene on. Returns a reason when it cannot be, the way a command ack wants it. */
  show(scene: SceneId): string | void;
  /** Takes it down. Returns a reason when nothing was showing. */
  hide(): string | undefined;
  /** The TV could not play the kept clip: drop it, and say so. */
  broken(): void;
  /** The TV could not play the starter: it is left out for this scene from now on. */
  starterFailed(): void;
}

/** Judged once: a set does not grow WebRTC or IndexedDB while the app runs. */
const CAPABLE = agentSupported();

const messageOf = (e: unknown) => (e instanceof Error && e.message ? e.message : 'Something went wrong.');
const nameOf = (id: SceneId) => SCENES[id].label.toLowerCase();

/** The scene on screen, and whether its clip was already kept when it was asked for. */
interface Shown {
  scene: SceneId;
  kept: boolean;
}

export function useAmbient(): AmbientView {
  // `kept` is decided at the request, not read from the store as it changes: a clip that
  // finishes while the starter plays is for the next request, not a cut to mid-scene.
  const [shown, setShown] = useState<Shown | null>(null);
  // Scenes kept on this TV. A clip's bytes are only read when it is on screen.
  const [made, setMade] = useState<string[]>([]);
  // Scenes whose starter this TV could not play (no file shipped, or a codec it lacks).
  const [noStarter, setNoStarter] = useState<SceneId[]>([]);
  const [stamp, setStamp] = useState(0); // moves when a kept clip is replaced by a new take
  const [running, setRunning] = useState<Running | null>(null);
  const [live, setLive] = useState<{ scene: SceneId; stream: MediaStream } | null>(null);
  const [clip, setClip] = useState<{ scene: SceneId; url: string; sound: boolean } | null>(null);
  const [problem, setProblem] = useState('');
  const agent = useMemo(() => new AmbientSceneAgent(PROXY_URL, RECORD_SECONDS), []);

  const scene = shown ? shown.scene : null;
  // For show() and hide(), which the command dispatcher calls from whichever render's closure it holds.
  const latest = useRef({ shown, made, running, problem });
  latest.current = { shown, made, running, problem };

  useEffect(() => {
    let alive = true;
    if (CAPABLE) listClipKeys().then((keys) => { if (alive) setMade(keys); });
    return () => { alive = false; agent.cancel(); };
  }, [agent]);

  const starter = scene !== null && STARTERS && !noStarter.includes(scene);
  // The kept clip plays when it was there at the request; and, with no starter to stand in, as
  // soon as it is made, taking over from the live stream that was showing meanwhile.
  const useKept = shown !== null && (shown.kept || (!starter && made.includes(shown.scene)));

  // ---------- the kept clip on screen ----------
  useEffect(() => {
    if (!useKept || !scene) return;
    let stale = false;
    let url = '';
    loadClip(scene).then((c) => {
      if (stale) return;
      // Gone from storage behind our back, or evicted for a newer one: it is simply not made.
      if (!c) { setMade((m) => m.filter((k) => k !== scene)); return; }
      url = URL.createObjectURL(c.blob);
      setClip({ scene, url, sound: c.sound });
      // The clip has taken over from the stream it was recorded from.
      setLive((l) => (l && l.scene === scene ? null : l));
    });
    // The URL dies with the effect, and so must the state that names it: asking for the same
    // scene again would otherwise hand the video a dead URL, whose error drops the kept clip.
    return () => { stale = true; if (url) { URL.revokeObjectURL(url); setClip(null); } };
  }, [useKept, scene, stamp]);

  // ---------- making one ----------
  const begin = useCallback((id: SceneId) => {
    const total = RECORD_SECONDS;
    setLive(null);
    setRunning({ scene: id, phase: 'connecting', seconds: 0, total });
    agent
      .create(id, {
        onProgress: (p) => setRunning({ scene: id, ...p }),
        onStream: (s) => setLive({ scene: id, stream: s }),
      })
      .then(async (m) => {
        const saved = await saveClip({ key: id, blob: m.blob, mime: m.mime, seconds: m.seconds, sound: m.sound, made: Date.now() });
        if (!saved) throw new AgentError('There is not enough room on this TV to keep it.');
        setMade((list) => (list.includes(id) ? list : [...list, id]));
        setStamp((n) => n + 1);
      })
      .catch((e) => {
        setLive((l) => (l && l.scene === id ? null : l));
        // Superseded by another scene, or the app closing: nobody is waiting for the news.
        if (e instanceof AgentError && e.message === CANCELLED) return;
        // Told on screen only when nothing else is showing there; the console always has it.
        console.warn('[ambient]', id, messageOf(e));
        const now = latest.current.shown;
        if (now && now.scene === id) setProblem(`Couldn't make the ${nameOf(id)}. ${messageOf(e)}`);
      })
      .finally(() => setRunning((r) => (r && r.scene === id ? null : r)));
  }, [agent]);

  const show = useCallback((id: SceneId): string | void => {
    if (!CAPABLE) return 'this TV cannot show ambient scenes';
    // The backend validates the scene, but the contract and this table could drift.
    if (!isScene(id)) return `unknown scene ${id}`;
    const now = latest.current;
    const kept = now.made.includes(id);
    const on = now.shown !== null && now.shown.scene === id;
    setProblem('');
    // Already on, and nothing fresher to show: asked for again while the starter plays and its
    // clip has since been kept, the request means the fresh one.
    if (on && !now.problem && (now.shown!.kept || !kept)) return;
    setShown({ scene: id, kept });
    if (kept) return; // the effect above plays it; no session
    if (now.running && now.running.scene === id) return; // still being made: back to what stands in
    begin(id);
  }, [begin]);

  // `?ambientScene=` stands in for the spoken command, so the flow can be driven without the
  // avatar. Once the kept clips are known, so a scene already made is not made again.
  const auto = useRef(AUTO_SCENE);
  useEffect(() => {
    if (!auto.current || !isScene(auto.current)) return;
    const id = auto.current;
    auto.current = null;
    listClipKeys().then((keys) => { setMade(keys); latest.current.made = keys; show(id); });
    // Mount only: the flag is a one-off, and `show` is stable.
  }, []);

  const hide = useCallback((): string | undefined => {
    if (!latest.current.shown) return 'no ambient scene is showing';
    setShown(null);
    setProblem('');
    // A scene still being made carries on to its end, off screen, and is kept: the session is
    // billed for its minute whether it is watched or not, so the next request should be free.
    return undefined;
  }, []);

  const broken = useCallback(() => {
    const now = latest.current.shown;
    if (!now) return;
    removeClip(now.scene);
    setMade((m) => m.filter((k) => k !== now.scene));
    setClip(null);
    setProblem(`The ${nameOf(now.scene)} could not be played on this TV, so it was removed. Ask for it again to make a new one.`);
  }, []);

  const starterFailed = useCallback(() => {
    const now = latest.current.shown;
    if (!now) return;
    console.warn('[ambient] no starter for', now.scene, '- run tools/make_ambient_starters.py');
    setNoStarter((list) => (list.includes(now.scene) ? list : [...list, now.scene]));
  }, []);

  let picture: Picture | null = null;
  if (scene) {
    if (useKept) picture = clip && clip.scene === scene ? { kind: 'kept', url: clip.url, sound: clip.sound } : null;
    else if (starter) picture = { kind: 'starter', url: starterUrl(scene) };
    else if (live && live.scene === scene) picture = { kind: 'live', stream: live.stream };
  }
  return {
    scene,
    picture,
    running: running && running.scene === scene ? running : null,
    problem,
    show,
    hide,
    broken,
    starterFailed,
  };
}

/**
 * The TV's command handler with the scene verbs added, and the verbs that a scene sits on top
 * of made aware of it: `close`, `back` and `home` take the scene down first, as the remote's
 * Back does for whatever is topmost, and `play` takes it down before the trailer opens. What
 * is on screen under the scene is left as the base handler had it.
 */
export function withAmbient(base: Omit<CommandHandler, 'show_ambient' | 'hide_ambient'>, ambient: AmbientView): CommandHandler {
  const closed = () => ambient.hide() === undefined;
  return {
    ...base,
    // A trailer under the scene would keep talking over it; "nothing is playing" is fine to ignore.
    show_ambient: ({ scene }) => { base.pause(); return ambient.show(scene); },
    hide_ambient: () => ambient.hide(),
    close: () => (closed() ? undefined : base.close()),
    back: () => (closed() ? undefined : base.back()),
    home: () => { closed(); return base.home(); },
    play: (args) => { closed(); return base.play(args); },
  };
}

/** The screen the TV reports, with the scene on it, so the agent knows what "close it" means. */
export function useAmbientScreen(screen: ScreenState, scene: SceneId | null): ScreenState {
  return useMemo(() => (scene ? { ...screen, ambient: scene } : screen), [screen, scene]);
}
