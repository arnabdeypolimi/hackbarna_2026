import { themeById, type Theme, type ThemeId } from '../lib/theme';
import { clipKey, weatherById, type Weather, type WeatherId } from './catalogue';
import type { Progress } from './agent';

/** The backdrop being made: which one, and how far along. */
export interface Running extends Progress {
  season: ThemeId;
  weather: WeatherId;
}

interface Props {
  theme: Theme;
  weathers: Weather[];
  /** The weather on screen for this sky; null is the season's own sky. */
  choice: WeatherId | null;
  /** Keys of the clips kept on this TV. */
  made: string[];
  running: Running | null;
  problem: string;
  /** False when the viewer set this sky to Still: nothing moves, so no weather is shown. */
  motion: boolean;
  onPick: (weather: WeatherId | null) => void;
  onAgain: () => void;
}

const nameOf = (id: WeatherId) => weatherById(id).label.toLowerCase();

/**
 * The picker's weather row, in the picker's own vocabulary: a label and a row of chips, one of
 * them current, the same classes as Room, Glass and Sky above it, so the remote treats it as
 * one of them. A weather that has not been made on this TV is outlined: pressing it makes it,
 * which takes about a minute; pressing one already made just plays it.
 */
export function WeatherRow({ theme, weathers, choice, made, running, problem, motion, onPick, onAgain }: Props) {
  const has = (w: WeatherId) => made.includes(clipKey(theme.id, w));
  const chip = (id: string, label: string, cur: boolean, say: string, act: () => void, extra = '') => (
    <button key={id} className={`tchip f${cur ? ' cur' : ''}${extra}`} aria-label={say} onClick={act}>
      {label}
    </button>
  );

  let note: string;
  if (problem) note = problem;
  else if (running) {
    const what = `${nameOf(running.weather)} over ${themeById(running.season).name}`;
    note = running.phase === 'connecting'
      ? `Creating ${what}. Connecting…`
      : `Creating ${what}: ${running.seconds} of ${running.total} s.`;
  } else if (choice && !has(choice)) note = `${weatherById(choice).label} is not made for ${theme.name} yet. Press it to create it; it takes about a minute.`;
  else if (choice) note = `${weatherById(choice).label} over ${theme.name}.`;
  else note = 'Pick a weather to create it for this sky. The first one takes about a minute.';
  // The refusal to start one already says so; anything else is told the sky is Still.
  if (!motion && !problem) note += ' Sky is set to Still, so nothing moves.';

  return (
    <>
      <div className="trow" role="group" aria-label="Weather">
        <span className="tkey" aria-hidden="true">Weather</span>
        {chip('none', 'None', choice === null, `No weather, the season's own sky${choice === null ? ', selected' : ''}`, () => onPick(null))}
        {weathers.map((w) => {
          const ready = has(w.id);
          const cur = choice === w.id;
          const say = `${w.label}${cur ? ', selected' : ''}${ready ? '' : ', not made yet, takes about a minute'}`;
          return chip(w.id, w.label, cur, say, () => onPick(w.id), ready ? '' : ' wx-new');
        })}
        {choice && chip('again', 'New take', false, `New take: make ${nameOf(choice)} again`, onAgain)}
      </div>
      <p className="tnote" role="status" aria-live="polite">{note}</p>
    </>
  );
}
