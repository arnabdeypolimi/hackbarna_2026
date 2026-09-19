import { forwardRef, useEffect, useState, type ReactNode } from 'react';

const W = 1920;
const H = 1080;

function fit() {
  const s = Math.min(window.innerWidth / W, window.innerHeight / H);
  return { s, x: (window.innerWidth - W * s) / 2, y: (window.innerHeight - H * s) / 2 };
}

/** A fixed 1920×1080 canvas scaled to the screen, so layout is identical on 720p and 1080p TVs. */
export const Stage = forwardRef<HTMLDivElement, { children: ReactNode }>(function Stage({ children }, ref) {
  const [box, setBox] = useState(fit);
  useEffect(() => {
    const onResize = () => setBox(fit());
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);
  return (
    <div id="stage" ref={ref} style={{ transform: `scale(${box.s})`, left: box.x, top: box.y }}>
      {children}
    </div>
  );
});
