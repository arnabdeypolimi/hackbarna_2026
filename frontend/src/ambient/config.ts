/**
 * Where the fal proxy runs. It is the theme agent's (src/tv_avatar/theme_agent, port 8010), the
 * same one the weather backdrops use: one key, one allowed model, one daily cap for both.
 */
export const PROXY_URL: string =
  import.meta.env.VITE_AMBIENT_PROXY_URL
  || import.meta.env.VITE_WEATHER_PROXY_URL
  || 'http://localhost:8010/fal/proxy';

/** The only model the proxy will spend the key on; the same one the weather uses. */
export const ENDPOINT = 'minimax/h3-max/director';

const query = new URLSearchParams(typeof location === 'undefined' ? '' : location.search);

/**
 * `?ambientMock=1` swaps fal for a canvas with a tone on it, so the whole flow — the spoken
 * command, the fullscreen scene, recording with sound, keeping, looping, closing — runs on a
 * desktop with no key and no bill.
 */
export const MOCK = query.get('ambientMock') === '1';

const seconds = Number(query.get('ambientSeconds'));

/**
 * How much of the stream is kept, and so how long the loop is. A session bills at least 60 s
 * however short it runs, so a full minute is the most that costs nothing extra; the lead-in
 * before the first frame is on top. `?ambientSeconds=` shortens it for a test run.
 */
export const RECORD_SECONDS = seconds >= 3 && seconds <= 110 ? Math.round(seconds) : 60;

/**
 * The starter clip for a scene: a short loop rendered once from the same prompt by fal's
 * text-to-video endpoint (tools/make_ambient_starters.py) and shipped with the app in
 * public/ambient/. It is what a scene opens with, so the viewer never waits on a session:
 * Director makes the full minute behind it. `?ambientStarter=0` leaves the starters out, to
 * watch the live path (and, with `ambientMock=1`, the mock's own picture).
 */
export const STARTERS = query.get('ambientStarter') !== '0';
export const starterUrl = (scene: string): string => `${import.meta.env.BASE_URL}ambient/${scene}.mp4`;

/**
 * `?ambientScene=fireplace` shows that scene on load, as if the viewer had asked the avatar for
 * it: the way to drive the flow with no avatar backend. Without `ambientMock=1` it opens a
 * real, paid session.
 */
export const AUTO_SCENE: string | null = query.get('ambientScene');
