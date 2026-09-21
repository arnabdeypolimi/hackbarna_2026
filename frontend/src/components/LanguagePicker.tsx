import type { LanguageInfo } from '../lib/avatarClient';

interface Props {
  options: LanguageInfo[];
  value: string;
  onPick: (code: string) => void;
}

/**
 * Chips, not a <select>: a native dropdown on a television opens a list the remote
 * cannot steer well. Each chip carries `.f`, which is all spatial navigation needs
 * to find it.
 *
 * Never disabled, not even mid-connection. The app connects on load, so a picker
 * that greys out while connecting is dead for the first seconds of every session
 * and forever if the backend hangs — and a disabled button is skipped by spatial
 * navigation, which would strand the whole panel. A press during a connection
 * supersedes it; `useAvatar`'s generation counter exists for exactly that.
 */
export function LanguagePicker({ options, value, onPick }: Props) {
  if (!options.length) return null;
  return (
    <div className="langs" role="group" aria-label="Avatar language">
      {options.map((l) => (
        <button
          key={l.code}
          className={l.code === value ? 'lang cur f' : 'lang f'}
          aria-label={`Speak ${l.name}`}
          aria-pressed={l.code === value}
          onClick={() => onPick(l.code)}
        >
          {l.native_name}
        </button>
      ))}
    </div>
  );
}
