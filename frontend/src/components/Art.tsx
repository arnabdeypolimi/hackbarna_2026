import { useEffect, useState } from 'react';
import type { Title } from '../types/title';
import { artBackground, posterFontSize } from '../lib/art';

type Load = 'loading' | 'ok' | 'failed';

/** Poster or backdrop image over a generated fallback. The title text hides once a real image loads. */
export function Art({ item, wide = false }: { item: Title; wide?: boolean }) {
  const src = wide ? item.backdrop || item.image : item.image;
  const [load, setLoad] = useState<Load>(src ? 'loading' : 'failed');
  useEffect(() => setLoad(src ? 'loading' : 'failed'), [src]);

  return (
    <div className={`art${load === 'ok' ? ' has-img' : ''}`} style={{ background: artBackground(item.title) }}>
      {src && load !== 'failed' && (
        <img src={src} alt="" loading="lazy" onLoad={() => setLoad('ok')} onError={() => setLoad('failed')} />
      )}
      {!wide && <span className="t" style={{ fontSize: posterFontSize(item.title) }}>{item.title}</span>}
    </div>
  );
}
