import { forwardRef, useEffect, useState, type ReactNode } from 'react';

const W = 1920;
const H = 1080;

function fit() {
  const s = Math.min(window.innerWidth / W, window.innerHeight / H);
  return { s, x: (window.innerWidth - W * s) / 2, y: (window.innerHeight - H * s) / 2 };
}

/**
 * A fixed 1920×1080 canvas scaled to the screen, so layout is identical on 720p and 1080p TVs.
 *
 * `flat` folds the room's perspective away for the duration of playback, and `solo` holds it
 * flat *and* gives the browse panel the whole screen until the avatar has answered. Both are
 * attributes rather than props threaded to every angled surface: the stylesheet keeps every
 * tilt, lift and solo offset in one place, and the two selectors there do the rest.
 */
export const Stage = forwardRef<HTMLDivElement, { children: ReactNode; flat?: boolean; solo?: boolean }>(
  function Stage({ children, flat, solo }, ref) {
    const [box, setBox] = useState(fit);
    useEffect(() => {
      const onResize = () => setBox(fit());
      window.addEventListener('resize', onResize);
      return () => window.removeEventListener('resize', onResize);
    }, []);
    return (
      <div
        id="stage"
        ref={ref}
        data-flat={flat ? '' : undefined}
        data-solo={solo ? '' : undefined}
        style={{ transform: `scale(${box.s})`, left: box.x, top: box.y }}
      >
        {children}
      </div>
    );
  },
);
