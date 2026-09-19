export type Dir = 'left' | 'right' | 'up' | 'down';

export const DIRS: Record<number, Dir> = { 37: 'left', 38: 'up', 39: 'right', 40: 'down' };

export function isVisible(el: Element): boolean {
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0;
}

/**
 * Picks the closest element in a direction. Elements that line up with the current one
 * (their ranges overlap on the cross axis) win over ones that are nearer but off to the side.
 */
export function findNext(from: Element, dir: Dir, candidates: Element[]): HTMLElement | null {
  const r = from.getBoundingClientRect();
  const cx = r.left + r.width / 2;
  const cy = r.top + r.height / 2;
  let best: HTMLElement | null = null;
  let bestScore = Infinity;

  for (const el of candidates) {
    const o = el.getBoundingClientRect();
    const ex = o.left + o.width / 2;
    const ey = o.top + o.height / 2;
    const gapY = Math.max(0, o.top - r.bottom, r.top - o.bottom);
    const gapX = Math.max(0, o.left - r.right, r.left - o.right);
    let main: number;
    let cross: number;
    if (dir === 'right') { if (o.left < r.right - 8) continue; main = o.left - r.right; cross = gapY || Math.abs(ey - cy) * 0.1; }
    else if (dir === 'left') { if (o.right > r.left + 8) continue; main = r.left - o.right; cross = gapY || Math.abs(ey - cy) * 0.1; }
    else if (dir === 'down') { if (o.top < r.bottom - 8) continue; main = o.top - r.bottom; cross = gapX || Math.abs(ex - cx) * 0.1; }
    else { if (o.bottom > r.top + 8) continue; main = r.top - o.bottom; cross = gapX || Math.abs(ex - cx) * 0.1; }
    const score = main + cross * 2.2;
    if (score < bestScore) { bestScore = score; best = el as HTMLElement; }
  }
  return best;
}
