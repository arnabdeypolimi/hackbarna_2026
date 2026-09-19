import type { RefObject } from 'react';
import type { AgentState, AvatarInfo, LanguageInfo } from '../lib/avatarClient';
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
  setLanguage: (code: string) => void;
  retry: () => void;
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
          <p className="say" aria-live="polite">{view.lastLine || 'Say hello.'}</p>
        </>
      ) : (
        <div className="side-actions">
          <button
            className="btn wide primary f"
            disabled={view.phase === 'connecting'}
            onClick={view.retry}
          >
            {view.phase === 'connecting' ? 'Connecting…' : `Talk to ${name}`}
          </button>
        </div>
      )}

      <LanguagePicker
        options={view.languages}
        value={view.language}
        disabled={view.phase === 'connecting'}
        onPick={view.setLanguage}
      />
    </aside>
  );
}
