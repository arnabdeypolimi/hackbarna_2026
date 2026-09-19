import type { Title } from '../types/title';

export function Meta({ item }: { item: Title }) {
  const parts = [item.typeLabel, item.years, item.rating, item.runtime, item.score ? `${item.score.toFixed(1)} / 10` : ''];
  return <div className="meta">{parts.filter(Boolean).map((p, i) => <span key={i}>{p}</span>)}</div>;
}

export function Chips({ values }: { values: string[] }) {
  return <div className="chips">{values.map((v) => <span className="chip" key={v}>{v}</span>)}</div>;
}
