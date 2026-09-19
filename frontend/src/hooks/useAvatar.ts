import { useCallback, useEffect, useRef, useState, type RefObject } from 'react';
import { avatarForLanguage, languageOptions } from '../lib/avatarCatalog';
import { connect, fetchConfig, type AgentState, type AvatarSession, type BackendConfig } from '../lib/avatarClient';
import { readJSON, writeJSON } from '../lib/storage';
import type { AvatarView, Phase } from '../components/AvatarPanel';

// A property of the television, not of a profile: whoever sits down next hears the
// language the set was left speaking.
const LANG_KEY = 'tv.avatar.language';

export function useAvatar(video: RefObject<HTMLVideoElement>): AvatarView {
  const [config, setConfig] = useState<BackendConfig | null>(null);
  const [phase, setPhase] = useState<Phase>('off');
  const [message, setMessage] = useState('Starting…');
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
        onStatus: (s) => { if (mineStill()) setStatus(s); },
        onTranscript: (m) => { if (mineStill() && m.text.trim()) setLastLine(m.text); },
        onCommand: (m) => {
          if (!mineStill()) return;
          // TODO(M3): dispatch verbs into App state. The agent emits no commands until
          // tool calls are wired — see "Current state" in CLAUDE.md and the verb list
          // in contracts/protocol.d.ts. The guard above is for that day: a superseded
          // session's late command must not reach the current screen.
          console.debug('[avatar] command', m.verb, m.args);
        },
        onError: (e) => {
          if (!mineStill()) return;
          // The panel is about to say we are not live, so we must not be: a hidden
          // <video> keeps playing audio, and the session keeps billing.
          session.current?.close();
          session.current = null;
          setPhase('error');
          setMessage(e.message);
        },
      });
      // A newer attempt started while this one was negotiating: drop this session
      // rather than leaving it running and unreferenced.
      if (!mineStill()) { live.close(); return; }
      session.current = live;
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

  useEffect(() => {
    mounted.current = true;
    void boot();
    return () => {
      mounted.current = false;
      gen.current++;
      session.current?.close();
      session.current = null;
    };
    // Mount only: re-running this would open a second session.
  }, []);

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
    setLanguage,
    retry,
  };
}
