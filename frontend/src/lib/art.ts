function hash(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
  return h >>> 0;
}

/** A stable two-tone backdrop per title, shown until (or instead of) the real image. */
export function artBackground(title: string): string {
  const h = hash(title);
  const a = h % 360;
  const b = (a + 30 + ((h >> 9) % 60)) % 360;
  const angle = (h >> 4) % 180;
  const x = 20 + ((h >> 3) % 60);
  const y = 10 + ((h >> 6) % 40);
  return `radial-gradient(120% 90% at ${x}% ${y}%, hsla(${b},60%,58%,.9), transparent 60%), linear-gradient(${angle}deg, hsl(${a},42%,16%), hsl(${a},38%,34%))`;
}

/** Sizes the title text so its longest word fits the poster width. */
export function posterFontSize(title: string): number {
  const longest = Math.max(...title.split(/\s+/).map((w) => w.length));
  return Math.round(Math.min(40, 160 / (longest * 0.74)));
}
