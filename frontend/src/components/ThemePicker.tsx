import { useEffect, type MouseEvent, type RefObject } from 'react';
import {
  THEMES, belowGrade, monthLabel, skyOf, themeAfter, type Step, type Theme, type ThemeChoice, type Tune,
} from '../lib/theme';
import { CalendarIcon } from './Icons';

interface Props {
  open: boolean;
  choice: ThemeChoice;
  /** The sky on screen right now, which is what the grid marks — auto or not. */
  theme: Theme;
  /** That sky's adjustments. */
  tune: Tune;
  scopeRef: RefObject<HTMLDivElement>;
  onPick: (choice: ThemeChoice) => void;
  onTune: (tune: Tune) => void;
  onClose: () => void;
}

const ROOM: [Step, string][] = [[-1, 'Lighter'], [0, 'Graded'], [1, 'Darker']];
const GLASS: [Step, string][] = [[-1, 'Clearer'], [0, 'Standard'], [1, 'Smokier']];

/**
 * The theme picker, opened by the green key or the disc in the tab bar.
 *
 * It sits in a panel over the dimmed room rather than behind the profiles' near-opaque scrim,
 * because the whole point of the screen is watching the room change behind it: a pick applies
 * at once and the overlay stays open, so two skies can be compared without reopening it. The
 * rows under the skies adjust the one on screen, and they apply the same way. Back closes it,
 * and so do the Done button and a click on the room outside the panel, for a pointer.
 */
export function ThemePicker({ open, choice, theme, tune, scopeRef, onPick, onTune, onClose }: Props) {
  const auto = choice === 'auto';

  // Opens on the sky that is showing, so the remote starts where the viewer is looking.
  useEffect(() => {
    if (!open) return;
    const scope = scopeRef.current;
    (scope?.querySelector<HTMLElement>('.ttile.cur') || scope?.querySelector<HTMLElement>('.ttile'))?.focus();
  }, [open, scopeRef]);

  if (!open) return null;

  const next = themeAfter(theme);
  const chip = (label: string, cur: boolean, say: string, pick: () => void) => (
    <button key={label} className={`tchip f${cur ? ' cur' : ''}`} aria-label={`${say}${cur ? ', selected' : ''}`} onClick={pick}>
      {label}
    </button>
  );
  const note = auto
    ? `${theme.name} now, ${next.name} from ${monthLabel(next.from)}.`
    : `${theme.name}, until you choose another.`;
  const onScrim = (e: MouseEvent<HTMLDivElement>) => { if (e.target === e.currentTarget) onClose(); };

  return (
    <div className="dialog themes" role="dialog" aria-modal="true" aria-labelledby="theme-head" ref={scopeRef} onClick={onScrim}>
      <div className="panel tpanel">
        <h2 id="theme-head">Theme</h2>
        <div className="tgrid">
          {THEMES.map((t) => {
            const cur = t.id === theme.id;
            const state = cur ? (auto ? ', showing now' : ', selected') : '';
            return (
              <button
                key={t.id}
                className={`ttile f${cur ? ' cur' : ''}`}
                aria-label={`${t.name}, ${t.season.toLowerCase()}${state}`}
                onClick={() => onPick(t.id)}
              >
                <span className="tsky" style={{ backgroundImage: skyOf(t) }} />
                <span className="tname">{t.name}</span>
                <span className="tseason">{t.season}</span>
              </button>
            );
          })}
        </div>
        {/* Turning it off pins whatever is on screen, so the button never does nothing. */}
        <button
          className={`btn f tauto${auto ? ' cur' : ''}`}
          aria-label={`Follow the season, ${auto ? 'on' : 'off'}`}
          onClick={() => onPick(auto ? theme.id : 'auto')}
        >
          <CalendarIcon />
          Follow the season
        </button>

        <p className="tsub">Adjust {theme.name}</p>
        {/* The labels sit beside the groups rather than inside them: one grid, so all three
            lines share a label column and a chip column. Centring each row on its own put
            them on three different axes, because the rows are not the same width. */}
        <div className="tadjust">
          <span className="tkey" aria-hidden="true">Room</span>
          <div className="trow" role="group" aria-label="Room">
            {ROOM.map(([v, label]) => chip(label, tune.room === v, `Room ${label.toLowerCase()}`, () => onTune({ ...tune, room: v })))}
          </div>
          <span className="tkey" aria-hidden="true">Glass</span>
          <div className="trow" role="group" aria-label="Glass">
            {GLASS.map(([v, label]) => chip(label, tune.glass === v, `Glass ${label.toLowerCase()}`, () => onTune({ ...tune, glass: v })))}
          </div>
          <span className="tkey" aria-hidden="true">Sky</span>
          <div className="trow" role="group" aria-label="Sky">
            {chip('Moving', tune.motion, 'Moving sky', () => onTune({ ...tune, motion: true }))}
            {chip('Still', !tune.motion, 'Still sky', () => onTune({ ...tune, motion: false }))}
          </div>
        </div>

        <p className="tnote">
          {note}
          {/* Said, not forbidden: the viewer is trading a little legibility for more of the room. */}
          {belowGrade(tune) && ' Lighter than graded: small text may be harder to read.'}
        </p>
        <button className="btn primary f tdone" aria-label="Done, close the theme picker" onClick={onClose}>Done</button>
      </div>
    </div>
  );
}
