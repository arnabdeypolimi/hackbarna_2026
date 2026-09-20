import type { RefObject } from 'react';
import type { AgentState, AvatarInfo, LanguageInfo, Outbound } from '../lib/avatarClient';
import { LanguagePicker } from './LanguagePicker';

export type Phase = 'off' | 'connecting' | 'live' | 'blocked' | 'error';

/** Everything the panel renders. Task 4's useAvatar hook returns exactly this. */
export interface AvatarView {
  phase: Phase;
  /** What the panel says while it is not live: a reason, never a blank rectangle. */
  message: string;
  avatar: AvatarInfo | null;
  status: AgentState;
  lastLine: string;
  languages: LanguageInfo[];
  language: string;
  /** The set's talking-head setting. Off still speaks; the face box says so. */
  videoEnabled: boolean;
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
  // The video element stays mounted and playing when the head is off — it carries the
  // voice. Only the picture is held back, so the box says what it is instead of going
  // black, and the name and state below it still report a live session.
  const showFace = live && view.videoEnabled;
  return (
    <aside className="panel side avatarpanel">
      <div className="face" data-state={showFace ? 'live' : 'off'}>
        <video ref={videoRef} autoPlay playsInline />
        {!showFace && <p className="face-note">{live ? 'Video off' : view.message}</p>}
      </div>

      {live ? (
        <>
          <p className="avatar-name">
            {name}
            <span className="pill" data-state={view.status}>{STATUS_LABEL[view.status]}</span>
          </p>
          <p className="say" aria-live="polite">{view.lastLine || 'Say hello.'}</p>
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
