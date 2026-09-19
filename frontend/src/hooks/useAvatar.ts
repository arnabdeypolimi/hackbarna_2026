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

    const mineStill = () => gen.current === mine;
    try {
      const live = await connect({
        avatar: who.id,
        language: code,
        video: el,
        onStatus: (s) => { if (mineStill()) setStatus(s); },
        onTranscript: (m) => { if (mineStill() && m.text.trim()) setLastLine(m.text); },
        onCommand: (m) => {
          // TODO(M3): dispatch verbs into App state. The agent emits no commands until
          // tool calls are wired — see "Current state" in CLAUDE.md and the verb list
          // in contracts/protocol.d.ts.
          console.debug('[avatar] command', m.verb, m.args);
        },
        onError: (e) => { if (mineStill()) { setPhase('error'); setMessage(e.message); } },
        onBlocked: () => { if (mineStill()) { setPhase('blocked'); setMessage(`Press OK to hear ${who.name}`); } },
      });
      // A newer attempt started while this one was negotiating: drop this session
      // rather than leaving it running and unreferenced.
      if (!mineStill()) { live.close(); return; }
      session.current = live;
      setPhase('live');
    } catch (err) {
      if (!mineStill()) return;
      const e = err as Error;
      // A refused microphone is recoverable by a gesture; everything else is not.
      const blocked = e.name === 'NotAllowedError' || e.name === 'SecurityError';
      setPhase(blocked ? 'blocked' : 'error');
      setMessage(blocked ? `Press OK to talk to ${who.name}` : e.message);
    }
  }, [video]);

  useEffect(() => {
    let cancelled = false;
    fetchConfig()
      .then((cfg) => {
        if (cancelled) return;
        setConfig(cfg);
        if (!cfg.configured) {
          setPhase('error');
          setMessage(`Avatar unavailable — missing ${cfg.missing.join(', ')}`);
          return;
        }
        const codes = languageOptions(cfg).map((l) => l.code);
        const code = codes.indexOf(language) >= 0 ? language : cfg.default_language;
        setLanguageState(code);
        void start(cfg, code);
      })
      .catch(() => {
        if (cancelled) return;
        setPhase('error');
        setMessage('Backend not running');
      });
    return () => {
      cancelled = true;
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
    if (!config || code === language) return;
    setLanguageState(code);
    writeJSON(LANG_KEY, code);
    void start(config, code);
  };

  const retry = () => { if (config) void start(config, language); };

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
