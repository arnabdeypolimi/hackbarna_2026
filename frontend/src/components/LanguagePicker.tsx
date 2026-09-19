import type { LanguageInfo } from '../lib/avatarClient';

interface Props {
  options: LanguageInfo[];
  value: string;
  disabled: boolean;
  onPick: (code: string) => void;
}

/**
 * Chips, not a <select>: a native dropdown on a television opens a list the remote
 * cannot steer well. Each chip carries `.f`, which is all spatial navigation needs
 * to find it.
 */
export function LanguagePicker({ options, value, disabled, onPick }: Props) {
  if (!options.length) return null;
  return (
    <div className="langs" role="group" aria-label="Avatar language">
      {options.map((l) => (
        <button
          key={l.code}
          className={l.code === value ? 'lang cur f' : 'lang f'}
          aria-label={`Speak ${l.name}`}
          aria-pressed={l.code === value}
          disabled={disabled}
          onClick={() => onPick(l.code)}
        >
          {l.native_name}
        </button>
      ))}
    </div>
  );
}
