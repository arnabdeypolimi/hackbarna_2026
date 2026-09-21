import type { CSSProperties } from 'react';

function hash(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
  return h >>> 0;
}

/** Which two of the theme's three card tints a title gets, and which way round. */
const PAIRS: [string, string][] = [['a', 'b'], ['b', 'c'], ['c', 'a'], ['b', 'a']];

/**
 * A stable backdrop per title, shown until (or instead of) the real image.
 *
 * The colours are the theme's: lib/theme.ts sets --card-a, --card-b, --card-c and --card-glow,
 * so a row of cards reads as one sky rather than a hand of playing cards. Only the angle, the
 * glow's place and the pair of tints belong to the title, which is enough to tell cards apart.
 */
export function artStyle(title: string): CSSProperties {
  const h = hash(title);
  const [a, b] = PAIRS[h % PAIRS.length];
  return {
    '--art-a': `var(--card-${a})`,
    '--art-b': `var(--card-${b})`,
    '--art-angle': `${(h >> 4) % 180}deg`,
    '--art-x': `${20 + ((h >> 3) % 60)}%`,
    '--art-y': `${10 + ((h >> 6) % 40)}%`,
  } as CSSProperties;
}

/** Sizes the title text so its longest word fits the poster width. */
export function posterFontSize(title: string): number {
  const longest = Math.max(...title.split(/\s+/).map((w) => w.length));
  return Math.round(Math.min(40, 160 / (longest * 0.74)));
}
