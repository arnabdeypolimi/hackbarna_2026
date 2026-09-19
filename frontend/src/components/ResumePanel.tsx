import type { Title } from '../types/title';
import { Art } from './Art';
import { ListIcon, PlayIcon } from './Icons';
import { Chips, Meta } from './Meta';

interface Props {
  item: Title | null;
  onContinue: () => void;
  onEpisodes: () => void;
  onRemind: () => void;
}

function startedLabel(item: Title): string {
  if (!item.last || item.position) return '';
  const when = new Date(item.last);
  if (when.toDateString() === new Date().toDateString()) return 'Started today';
  return `Started ${when.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}`;
}

export function ResumePanel({ item, onContinue, onEpisodes, onRemind }: Props) {
  if (!item) {
    return (
      <aside className="panel side">
        <h2>You watched last time</h2>
        <div className="empty side-empty">
          <b>Nothing in progress</b>
          Press Watch on any title and it will show up here, so you can pick up where you left off.
        </div>
      </aside>
    );
  }

  const stops = [
    item.season && `Season ${item.season}`,
    item.episode && `Episode ${item.episode}`,
    item.position,
    startedLabel(item),
  ].filter(Boolean) as string[];

  return (
    <aside className="panel side">
      <h2>You watched last time</h2>
      <div className="resume-art"><Art item={item} wide /></div>
      <h3>{item.title}</h3>
      <Meta item={item} />
      <Chips values={item.genres} />
      <p className="desc">{item.desc}</p>
      {stops.length > 0 && (<><p className="label">You stopped on</p><Chips values={stops} /></>)}
      <div className="side-actions">
        <button className="btn wide primary f" onClick={onContinue}><PlayIcon />Continue watching</button>
        {item.kind === 'series' && (
          <button className="btn wide f" onClick={onEpisodes}><ListIcon />Select another episode</button>
        )}
        <button className="link f" onClick={onRemind}>Remind me later</button>
      </div>
    </aside>
  );
}
