import { useCallback, useEffect, useRef, useState, type RefObject } from 'react';
import { avatarForLanguage, languageOptions } from '../lib/avatarCatalog';
import {
  connect, fetchConfig, type AgentState, type AvatarSession, type BackendConfig, type Outbound,
} from '../lib/avatarClient';
import { readJSON, writeJSON } from '../lib/storage';
import type { AvatarView, Phase } from '../components/AvatarPanel';
import { dispatchCommand, type CommandHandler } from './useTvControl';

// A property of the television, not of a profile: whoever sits down next hears the
// language the set was left speaking.
const LANG_KEY = 'tv.avatar.language';
// The same kind of property, and read by "Who's watching?" before any session exists,
// which is why loading it does not belong to this hook alone.
const VIDEO_KEY = 'tv.avatar.video';

export const loadAvatarVideo = (): boolean => readJSON<boolean>(VIDEO_KEY, true);
export const saveAvatarVideo = (on: boolean): void => writeJSON(VIDEO_KEY, on);

// The talking head is a property of the session, not of the element showing it: the offer
// carries `avatar=false` and the backend builds the pipeline without one, so nothing is
// generated and no Anam session is minted. Turning it on or off therefore needs a new
// session, the same as changing language does.

export interface AvatarOptions {
  /** The viewer's profile id; a change restarts the session under the new identity. */
  userId: string;
  /**
   * Whether a viewer has been chosen yet. The app opens on "Who's watching?", and a session
   * is a paid one under a particular identity, so there is nothing to connect for until
   * somebody has said who they are. It also saves a session: connecting on mount and then
   * picking a profile used to mint one under the restored id and immediately replace it.
   */
  ready: boolean;
  /** Whether the talking head is shown. Off keeps the voice and stops the decode. */
  videoEnabled: boolean;
  /**
   * What the TV does with each command. A ref, not a value: App reassigns it every render
   * so the socket callback always sees the latest closures without reconnecting.
   */
  commands: RefObject<CommandHandler | null>;
}

export function useAvatar(video: RefObject<HTMLVideoElement>, opts: AvatarOptions): AvatarView {
  const [config, setConfig] = useState<BackendConfig | null>(null);
  const [phase, setPhase] = useState<Phase>('off');
  const [message, setMessage] = useState('Choose who is watching to start.');
  const [status, setStatus] = useState<AgentState>('idle');
  const [lastLine, setLastLine] = useState('');
  const [language, setLanguageState] = useState(() => readJSON<string>(LANG_KEY, ''));

  const session = useRef<AvatarSession | null>(null);
  // One connection at a time. A language switch supersedes whatever the previous
  // attempt was doing, and that attempt's late callbacks must not write over it.
  const gen = useRef(0);
  const mounted = useRef(true);
  // The chosen language, readable from callbacks that must not be rebuilt every
  // time it changes.
  const lang = useRef(language);
  const user = useRef(opts.userId);
  user.current = opts.userId;
  // Read from the connect callback, which must not be rebuilt when the setting changes.
  const wantVideo = useRef(opts.videoEnabled);
  wantVideo.current = opts.videoEnabled;
  const commands = opts.commands;

  const choose = (code: string) => {
    lang.current = code;
    setLanguageState(code);
  };

  const start = useCallback(async (cfg: BackendConfig, code: string) => {
    const mine = ++gen.current;
    session.current?.close();
    session.current = null;
    setStatus('idle');
    setLastLine('');

    const who = avatarForLanguage(cfg, code);
    if (!who) {
      setPhase('error');
      setMessage(`No avatar speaks ${code}`);
      return;
    }
    const el = video.current;
    if (!el) {
      setPhase('error');
      setMessage('No video element');
      return;
    }
    setPhase('connecting');
    setMessage(`Connecting to ${who.name}…`);

    const mineStill = () => gen.current === mine && mounted.current;
    try {
      const live = await connect({
        avatar: who.id,
        language: code,
        userId: user.current,
        video: wantVideo.current,
        onStatus: (s) => { if (mineStill()) setStatus(s); },
        onTranscript: (m) => { if (mineStill() && m.text.trim()) setLastLine(m.text); },
        onCommand: (m) => {
          // A superseded session's late command must not reach the current screen. The
          // socket is opened inside connect(), so by the time a command can arrive
          // session.current is this session.
          if (!mineStill() || !commands.current || !session.current) return;
          dispatchCommand(m, commands.current, session.current.send);
        },
        onError: (e) => {
          if (!mineStill()) return;
          // The panel is about to say we are not live, so we must not be: a hidden
          // <video> keeps playing audio, and the session keeps billing.
          session.current?.close();
          session.current = null;
          // Retire this generation too. The error can arrive while connect() is still
          // negotiating, when there is no session to close yet — without this, that
          // connect() resolves afterwards, passes mineStill(), and overwrites the
          // error with `live`. Failing the check instead routes it to the branch that
          // closes the late session.
          gen.current++;
          setPhase('error');
          setMessage(e.message);
        },
      });
      // A newer attempt started while this one was negotiating: drop this session
      // rather than leaving it running and unreferenced.
      if (!mineStill()) { live.close(); return; }
      session.current = live;
      // Attached either way: with no avatar the stream is the voice alone.
      el.srcObject = live.stream;
      // Autoplay may be refused when nothing the viewer did started this — the app
      // connects on load, so that is the normal case, not the exception. The session is
      // healthy; only playback was blocked, so the recovery is a gesture, never a new
      // session.
      el.play().then(
        () => { if (mineStill()) setPhase('live'); },
        () => { if (mineStill()) { setPhase('blocked'); setMessage(`Press OK to hear ${who.name}`); } },
      );
    } catch (err) {
      if (!mineStill()) return;
      const e = err as Error;
      // A refused microphone is recoverable by a gesture; everything else is not.
      const blocked = e.name === 'NotAllowedError' || e.name === 'SecurityError';
      setPhase(blocked ? 'blocked' : 'error');
      setMessage(blocked ? `Press OK to talk to ${who.name}` : e.message);
    }
  }, [video]);

  /**
   * Fetch the catalogue, then connect. This is both the mount path and what the
   * panel's button re-runs, which is why it re-reads `/config` every time: the two
   * states it can report — a backend that is down, and one running without provider
   * keys — are both fixed outside the browser, and a viewer pressing OK is asking
   * whether that has happened yet.
   */
  const boot = useCallback(async () => {
    const mine = ++gen.current;
    session.current?.close();
    session.current = null;
    setPhase('connecting');
    setMessage('Starting…');

    let cfg: BackendConfig;
    try {
      cfg = await fetchConfig();
    } catch {
      if (gen.current !== mine || !mounted.current) return;
      setPhase('error');
      setMessage('Backend not running');
      return;
    }
    if (gen.current !== mine || !mounted.current) return;
    setConfig(cfg);

    // Settle the language before any early return can skip it. Everything downstream
    // reads it — the button most of all — and leaving it '' is what turns the useful
    // "missing keys" message into "No avatar speaks " on the first press.
    const codes = languageOptions(cfg).map((l) => l.code);
    choose(codes.indexOf(lang.current) >= 0 ? lang.current : cfg.default_language);

    if (!cfg.configured) {
      setPhase('error');
      setMessage(`Avatar unavailable — missing ${cfg.missing.join(', ')}`);
      return;
    }
    await start(cfg, lang.current);
  }, [start]);

  // Boots once, on the first render where a viewer has been chosen — not on mount, because
  // until then there is no identity to open a session under. Guarded by a ref rather than the
  // dependency list: `ready` can go false again (the picker reopens to switch viewer) and that
  // must not tear down or re-open a live session.
  const booted = useRef(false);
  useEffect(() => {
    mounted.current = true;
    if (opts.ready && !booted.current) {
      booted.current = true;
      void boot();
    }
    return () => {
      mounted.current = false;
    };
  }, [opts.ready, boot]);

  // Closing the session belongs to unmount alone; the effect above reruns when `ready` moves.
  useEffect(() => () => {
    gen.current++;
    session.current?.close();
    session.current = null;
  }, []);

  // A different viewer is a different backend identity (history, memory), so the session
  // restarts like it does for a language change. Skipped on mount — boot() covers that —
  // and while there is nothing live to replace.
  const firstUser = useRef(true);
  useEffect(() => {
    if (firstUser.current) { firstUser.current = false; return; }
    if (config && config.configured && session.current) void start(config, lang.current);
  }, [opts.userId]);

  // Reopening "Who's watching?" mid-session and flipping this rebuilds the pipeline, exactly
  // as a language change does. Skipped on the first run — boot() has not happened yet on the
  // launch path, and when it has there is nothing to replace.
  const firstVideo = useRef(true);
  useEffect(() => {
    if (firstVideo.current) { firstVideo.current = false; return; }
    if (config && config.configured && session.current) void start(config, lang.current);
  }, [opts.videoEnabled]);

  // Stable across renders so effects keyed on it (the screen-state push) do not refire.
  const send = useCallback((msg: Outbound) => { session.current?.send(msg); }, []);

  useEffect(() => {
    // A closed tab must not leave a session (and its provider minutes) running.
    const bye = () => session.current?.close();
    window.addEventListener('beforeunload', bye);
    return () => window.removeEventListener('beforeunload', bye);
  }, []);

  const setLanguage = (code: string) => {
    if (code === lang.current) return;
    choose(code);
    writeJSON(LANG_KEY, code);
    // Go back to the top unless there is a catalogue worth starting from. An
    // unconfigured backend has one, but connecting against it only strands the panel
    // in "Connecting…", where boot() says plainly what is missing.
    if (config && config.configured) void start(config, code);
    else void boot();
  };

  const retry = () => {
    const el = video.current;
    // A blocked session is alive and only needs the gesture we just got; re-booting would
    // tear down a healthy session and mint a new paid one. The other way into `blocked` is
    // a refused microphone, which leaves session.current null — that one does need a new
    // session, and falls through to boot().
    if (phase === 'blocked' && session.current && el) {
      el.play().then(() => setPhase('live'), () => { /* still blocked; the message stands */ });
      return;
    }
    void boot();
  };

  return {
    phase,
    message,
    avatar: config ? avatarForLanguage(config, language) : null,
    status,
    lastLine,
    languages: config ? languageOptions(config) : [],
    language,
    videoEnabled: opts.videoEnabled,
    setLanguage,
    retry,
    send,
  };
}
