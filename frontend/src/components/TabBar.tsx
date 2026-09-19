import type { Profile, Tab } from '../types/title';
import { initials } from '../lib/profiles';
import { skyOf, type Theme } from '../lib/theme';

const TABS: { id: Tab; label: string }[] = [
  { id: 'popular', label: 'Popular' },
  { id: 'top', label: 'Top rated' },
  { id: 'recent', label: 'New releases' },
  { id: 'list', label: 'My List' },
];

interface Props {
  tab: Tab;
  highlight: boolean;
  profile: Profile;
  /** The sky on screen, shown as a disc on the button that opens the picker. */
  theme: Theme;
  onSelect: (t: Tab) => void;
  onProfile: () => void;
  onTheme: () => void;
}

export function TabBar({ tab, highlight, profile, theme, onSelect, onProfile, onTheme }: Props) {
  const kids = profile.kind === 'kids';
  return (
    <nav className="panel tabs">
      {TABS.map((t) => (
        <button key={t.id} className={`tab f${highlight && tab === t.id ? ' cur' : ''}`} onClick={() => onSelect(t.id)}>
          {t.label}
        </button>
      ))}
      {/* The same picker the green key opens, for a mouse, a keyboard, or a remote without colour keys. */}
      <button className="skyknob f" aria-label={`Theme: ${theme.name}, ${theme.season.toLowerCase()} — change theme`} onClick={onTheme}>
        <span className="skydisc" style={{ backgroundImage: skyOf(theme) }} />
      </button>
      <button
        className={`avatar f${kids ? ' kids' : ''}`}
        style={{ background: profile.color }}
        aria-label={`${profile.name}${kids ? ', kids profile' : ''} — switch profile`}
        onClick={onProfile}
      >
        {initials(profile.name)}
        {kids && <span className="ktag">Kids</span>}
      </button>
    </nav>
  );
}
