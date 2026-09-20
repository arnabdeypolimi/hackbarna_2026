import type { RefObject } from 'react';
import type { AgentState, AvatarInfo, LanguageInfo, Outbound } from '../lib/avatarClient';
import { LanguagePicker } from './LanguagePicker';

export type Phase = 'off' | 'connecting' | 'live' | 'blocked' | 'error';

/**
 * The two sides of the exchange, kept apart. One shared line let the viewer's half-heard
 * words and the avatar's reply overwrite each other, so a misrecognised word looked like
 * the avatar ignoring the viewer instead of mishearing them.
 */
export interface Captions {
  /** The viewer's words as the recogniser hears them, live. */
  heard: string;
  /** False while `heard` is still an interim guess the recogniser may revise. */
  settled: boolean;
  /** The avatar's current reply, growing a sentence at a time as it is spoken. */
  reply: string;
}

export const EMPTY_CAPTIONS: Captions = { heard: '', settled: true, reply: '' };

/** Everything the panel renders. Task 4's useAvatar hook returns exactly this. */
export interface AvatarView {
  phase: Phase;
  /** What the panel says while it is not live: a reason, never a blank rectangle. */
  message: string;
  avatar: AvatarInfo | null;
  status: AgentState;
  captions: Captions;
  languages: LanguageInfo[];
  language: string;
  setLanguage: (code: string) => void;
  retry: () => void;
  /** Client→server messages (screen state, user events). A no-op while not live. */
  send: (msg: Outbound) => void;
}

interface Props {
  view: AvatarView;
  videoRef: RefObject<HTMLVideoElement>;
}

const STATUS_LABEL: Record<AgentState, string> = {
  idle: 'ready',
  listening: 'listening',
  thinking: 'thinking',
  speaking: 'speaking',
};

export function AvatarPanel({ view, videoRef }: Props) {
  const live = view.phase === 'live';
  const name = view.avatar?.name ?? 'the avatar';
  return (
    <aside className="panel side avatarpanel">
      <div className="face" data-state={live ? 'live' : 'off'}>
        <video ref={videoRef} autoPlay playsInline />
        {!live && <p className="face-note">{view.message}</p>}
      </div>

      {live ? (
        <>
          <p className="avatar-name">
            {name}
            <span className="pill" data-state={view.status}>{STATUS_LABEL[view.status]}</span>
          </p>
          {/* The viewer's own words are not announced: they change on every interim and the
              viewer just said them. The reply is. */}
          <dl className="captions">
            <dt>You</dt>
            <dd className="say heard" data-settled={view.captions.settled}>
              <span>{view.captions.heard || 'Say hello.'}</span>
            </dd>
            <dt>{name}</dt>
            <dd className="say reply" aria-live="polite"><span>{view.captions.reply}</span></dd>
          </dl>
        </>
      ) : (
        <div className="side-actions">
          {/* Guarded rather than disabled: spatial navigation skips a disabled control, and
              disabling the focused button drops focus to <body>, which ejects the viewer
              from the panel mid-press. The chips are never disabled for the same reason. */}
          <button
            className="btn wide primary f"
            onClick={() => { if (view.phase !== 'connecting') view.retry(); }}
          >
            {view.phase === 'connecting' ? 'Connecting…' : `Talk to ${name}`}
          </button>
        </div>
      )}

      <LanguagePicker
        options={view.languages}
        value={view.language}
        onPick={view.setLanguage}
      />
    </aside>
  );
}
