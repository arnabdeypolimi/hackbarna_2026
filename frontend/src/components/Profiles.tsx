import { useEffect, useState, type MutableRefObject, type RefObject } from 'react';
import type { Profile, ProfileKind } from '../types/title';
import {
  AVATAR_COLORS, KIND_LABEL, MAX_PROFILES, NAME_MAX, adultCount, initials, newProfile,
} from '../lib/profiles';
import { CheckIcon, CloseIcon, PencilIcon, PlusIcon, TrashIcon, VideoIcon, VideoOffIcon } from './Icons';
import type { ToastKind } from './Toast';

const KINDS: ProfileKind[] = ['adult', 'kids'];
const KIND_NOTE: Record<ProfileKind, string> = {
  adult: 'Every title in the catalogue.',
  kids: 'Only titles rated for children. Its own My List and history, like any profile.',
};

interface Props {
  open: boolean;
  profiles: Profile[];
  activeId: string;
  scopeRef: RefObject<HTMLDivElement>;
  /** App calls this on Back; returns true when the overlay handled it itself. */
  backRef: MutableRefObject<() => boolean>;
  onPick: (id: string) => void;
  onSave: (list: Profile[]) => void;
  onDelete: (id: string) => void;
  onNotice: (message: string, kind?: ToastKind) => void;
  /** The set's talking-head setting. Off builds the session without an avatar at all. */
  avatarVideo: boolean;
  onAvatarVideo: (on: boolean) => void;
}

/**
 * The "Who's watching?" picker. Three views share one overlay: the tile grid, the same
 * grid in manage mode, and the form for one profile. Back unwinds them in that order,
 * which is why App asks the overlay first before closing it.
 */
export function Profiles({
  open, profiles, activeId, scopeRef, backRef, onPick, onSave, onDelete, onNotice,
  avatarVideo, onAvatarVideo,
}: Props) {
  const [manage, setManage] = useState(false);
  const [draft, setDraft] = useState<Profile | null>(null);
  const [adding, setAdding] = useState(false);
  const editing = draft !== null;

  backRef.current = () => {
    if (editing) { setDraft(null); return true; }
    if (manage) { setManage(false); return true; }
    return false;
  };

  // Every view opens on the thing you came to use: the field, or the tile you were on.
  useEffect(() => {
    if (!open) return;
    const scope = scopeRef.current;
    const target = editing
      ? scope?.querySelector<HTMLElement>('#profile-name')
      : scope?.querySelector<HTMLElement>('.ptile.cur') || scope?.querySelector<HTMLElement>('.ptile');
    target?.focus();
  }, [open, editing, manage, scopeRef]);

  // Reopening the overlay always lands back on the plain picker.
  useEffect(() => {
    if (!open) { setManage(false); setDraft(null); setAdding(false); }
  }, [open]);

  if (!open) return null;

  const edit = (p: Profile) => { setAdding(false); setDraft({ ...p }); };
  const add = () => { setAdding(true); setDraft(newProfile(profiles)); };

  const commit = () => {
    if (!draft) return;
    const name = draft.name.trim();
    if (!name) { onNotice('Give the profile a name first', 'alert'); return; }
    const next = adding
      ? [...profiles, { ...draft, name }]
      : profiles.map((p) => (p.id === draft.id ? { ...draft, name } : p));
    // Turning the last adult into a kid would shut the full catalogue away for good.
    if (!adultCount(next)) { onNotice('Keep at least one adult profile', 'alert'); return; }
    onSave(next);
    setDraft(null);
  };

  const remove = () => {
    if (!draft) return;
    if (!adultCount(profiles.filter((p) => p.id !== draft.id))) {
      onNotice('Keep at least one adult profile', 'alert');
      return;
    }
    onDelete(draft.id);
    setDraft(null);
  };

  if (draft) {
    return (
      <div className="profiles" role="dialog" aria-modal="true" aria-labelledby="profile-head" ref={scopeRef}>
        <div className="panel pform">
          <h2 id="profile-head">{adding ? 'Add profile' : 'Edit profile'}</h2>
          <div className="pformtop">
            <span className="pface" style={{ background: draft.color }}>{initials(draft.name)}</span>
            <span className="field">
              <input
                id="profile-name"
                className="f"
                aria-label="Profile name"
                value={draft.name}
                maxLength={NAME_MAX}
                placeholder="Name"
                autoComplete="off"
                onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); commit(); } }}
              />
            </span>
          </div>
          <p className="label">Colour</p>
          <div className="swatches">
            {AVATAR_COLORS.map((c) => (
              <button
                key={c.value}
                className={`swatch f${c.value === draft.color ? ' cur' : ''}`}
                style={{ background: c.value }}
                aria-label={c.name}
                onClick={() => setDraft({ ...draft, color: c.value })}
              />
            ))}
          </div>
          <p className="label">Profile type</p>
          <div className="pkinds">
            {KINDS.map((k) => (
              <button
                key={k}
                className={`kind f${draft.kind === k ? ' cur' : ''}`}
                aria-label={`${KIND_LABEL[k]} profile`}
                onClick={() => setDraft({ ...draft, kind: k })}
              >
                {KIND_LABEL[k]}
              </button>
            ))}
          </div>
          <p className="pnote">{KIND_NOTE[draft.kind]}</p>
          <p className="pid">User ID <b>{draft.id}</b></p>
          <div className="actions">
            <button className="btn f" onClick={commit}><CheckIcon />{adding ? 'Add profile' : 'Save'}</button>
            {!adding && profiles.length > 1 && (
              <button className="btn danger f" onClick={remove}><TrashIcon />Delete</button>
            )}
            <button className="btn f" onClick={() => setDraft(null)}><CloseIcon />Cancel</button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="profiles" role="dialog" aria-modal="true" aria-labelledby="profile-head" ref={scopeRef}>
      <h2 id="profile-head">{manage ? 'Manage profiles' : "Who's watching?"}</h2>
      <div className="pgrid">
        {profiles.map((p) => {
          const who = p.kind === 'kids' ? `${p.name}, kids profile` : p.name;
          return (
            <button
              key={p.id}
              className={`ptile f${p.id === activeId ? ' cur' : ''}`}
              aria-label={manage ? `Edit ${who}` : `Watch as ${who}`}
              onClick={() => (manage ? edit(p) : onPick(p.id))}
            >
              <span className="pface" style={{ background: p.color }}>
                {initials(p.name)}
                {manage && <span className="pmask"><PencilIcon /></span>}
                {p.kind === 'kids' && <span className="ptag">Kids</span>}
              </span>
              <span className="pname">{p.name}</span>
            </button>
          );
        })}
        {profiles.length < MAX_PROFILES && (
          <button className="ptile f" aria-label="Add profile" onClick={add}>
            <span className="pface add"><PlusIcon /></span>
            <span className="pname">Add profile</span>
          </button>
        )}
      </div>
      <button className="btn f pmanage" onClick={() => setManage(!manage)}>
        {manage ? <><CheckIcon />Done</> : <><PencilIcon />Manage profiles</>}
      </button>
      {/* The set's own setting, in the corner rather than among the viewers: it belongs to
          the television, and this is the screen everyone passes through on the way in. The
          icon carries the state as well as the fill — the corner has no label beside it. */}
      <button
        className={`btn f pvideo${avatarVideo ? ' cur' : ''}`}
        aria-label={`Avatar video, ${avatarVideo ? 'on' : 'off'}`}
        aria-pressed={avatarVideo}
        onClick={() => onAvatarVideo(!avatarVideo)}
      >
        {avatarVideo ? <VideoIcon /> : <VideoOffIcon />}
        Avatar video
      </button>
    </div>
  );
}
