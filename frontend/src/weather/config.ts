/** Where the theme agent's fal proxy runs: src/tv_avatar/theme_agent, on its own port. */
export const PROXY_URL: string = import.meta.env.VITE_WEATHER_PROXY_URL || 'http://localhost:8010/fal/proxy';

/** The only model the agent asks for; the proxy refuses any other. */
export const ENDPOINT = 'minimax/h3-max/director';

const query = new URLSearchParams(typeof location === 'undefined' ? '' : location.search);

/**
 * `?weatherMock=1` swaps fal for a canvas that draws the weather, so the whole flow — picker,
 * recording, storing, playing — can be driven on a desktop with no key and no bill.
 */
export const MOCK = query.get('weatherMock') === '1';

const seconds = Number(query.get('weatherSeconds'));

/**
 * How much of the stream is kept. A session bills at least 60 s however short it runs, so a
 * clip is as long as it can be while the wait for the first frame still lands inside that
 * minute. `?weatherSeconds=` shortens it for a test run.
 */
export const RECORD_SECONDS = seconds >= 3 && seconds <= 90 ? Math.round(seconds) : 40;
