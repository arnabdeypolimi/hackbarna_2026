import type { Title } from '../types/title';
import { Art } from './Art';
import { HeartIcon, PlayIcon } from './Icons';
import { Chips, Meta } from './Meta';

interface Props {
  item: Title;
  saved: boolean;
  onWatch: () => void;
  onSave: () => void;
  onTrailer: () => void;
}

export function Detail({ item, saved, onWatch, onSave, onTrailer }: Props) {
  return (
    <div className="detail">
      <div className="info">
        <h3>{item.title}</h3>
        <Meta item={item} />
        <Chips values={item.genres} />
        <p className="desc">{item.desc}</p>
        <div className="actions">
          <button className="btn primary f" data-role="watch" onClick={onWatch}><PlayIcon />Watch</button>
          <button className={`btn f${saved ? ' saved' : ''}`} data-role="save" onClick={onSave}>
            <HeartIcon />{saved ? 'Saved to My List' : 'Save to My List'}
          </button>
        </div>
      </div>
      <button className="trailer f" aria-label={`Watch trailer for ${item.title}`} onClick={onTrailer}>
        <Art item={item} wide />
        <div className="scrim" />
        <div className="bar">
          <span className="pill"><PlayIcon />Watch trailer</span>
          <span>{item.trailer}</span>
        </div>
      </button>
    </div>
  );
}
