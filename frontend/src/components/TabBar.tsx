import type { Profile, Tab } from '../types/title';
import { initials } from '../lib/profiles';

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
  onSelect: (t: Tab) => void;
  onProfile: () => void;
}

export function TabBar({ tab, highlight, profile, onSelect, onProfile }: Props) {
  const kids = profile.kind === 'kids';
  return (
    <nav className="panel tabs">
      {TABS.map((t) => (
        <button key={t.id} className={`tab f${highlight && tab === t.id ? ' cur' : ''}`} onClick={() => onSelect(t.id)}>
          {t.label}
        </button>
      ))}
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
