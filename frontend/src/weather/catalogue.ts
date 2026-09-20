import type { ThemeId } from '../lib/theme';

export type WeatherId = 'clear' | 'cloudy' | 'rain' | 'storm' | 'snow' | 'fog';

export interface Weather {
  id: WeatherId;
  label: string;
}

export const WEATHERS: Weather[] = [
  { id: 'clear', label: 'Clear' },
  { id: 'cloudy', label: 'Cloudy' },
  { id: 'rain', label: 'Rain' },
  { id: 'storm', label: 'Storm' },
  { id: 'snow', label: 'Snow' },
  { id: 'fog', label: 'Fog' },
];

export const weatherById = (id: WeatherId): Weather => WEATHERS.find((w) => w.id === id) || WEATHERS[0];

/** Snow is left out of the seasons it does not belong to rather than painted anyway. */
const NOT_IN: Partial<Record<ThemeId, WeatherId[]>> = {
  blossom: ['snow'],
  blue: ['snow'],
  golden: ['snow'],
};

export const weathersFor = (season: ThemeId): Weather[] =>
  WEATHERS.filter((w) => !(NOT_IN[season] || []).includes(w.id));

export const offers = (season: ThemeId, weather: WeatherId): boolean =>
  weathersFor(season).some((w) => w.id === weather);

/** One clip per season and weather; the season is the sky's id, so the two never drift apart. */
export const clipKey = (season: ThemeId, weather: WeatherId): string => `${season}:${weather}`;

/**
 * The scene is the weather's, not the season's: each weather brings the landscape that shows
 * it best, and the sky picked in the theme dialog has no say in the picture. Only the picture
 * is described. Director's audio track is a soundtrack the caller supplies (`audio_url`),
 * played as it is, and silence without one, so words about sound would draw nothing.
 * Words the mock reads (mock.ts sceneOf): storm, snowfall, overcast, rain, fog.
 */
const SCENE: Record<WeatherId, string> = {
  clear:
    'A wide open landscape under a vast clear blue sky, a few thin white clouds drifting slowly, warm golden ' +
    'sunlight over rolling green hills and a distant tree line, long grass swaying gently in a light breeze.',
  cloudy:
    'Rolling moorland under a heavy overcast sky, thick layered grey clouds moving slowly, soft diffuse light, ' +
    'heather and long grass stirring in the wind, low hills far behind.',
  rain:
    'Steady rain falling over a lush green valley, fine droplets streaking through soft grey light, raindrops ' +
    'beading and dripping from broad wet leaves in the foreground, a hazy tree line beyond, ripples spreading ' +
    'across a still pool.',
  storm:
    'Dark towering storm clouds rolling across a wide plain, wind bending the trees and grass, lightning ' +
    'flickering deep inside the clouds and now and then forking down far away, heavy rain beginning to sweep ' +
    'across the frame.',
  snow:
    'Heavy snowfall over a silent pine forest, large soft flakes drifting slowly down, snow settling on the ' +
    'branches and a white field, cold blue-white light under low clouds.',
  fog:
    'Thick low fog over a still lake at dawn, soft pearl light, the far shore and bare trees fading into the ' +
    'fog, slow wisps drifting across the water.',
};

/**
 * Said the same way every time. This is footage for a television backdrop, not a film: it has
 * to look real, and it has to stay out of the way. One locked shot with no people and no cuts,
 * because the clip loops behind the room; the sky in the top half because that is where the
 * season's own sky sits, and where the tab bar and the posters are not. Legibility is not asked
 * of the picture: the scrim (floorOf() in WeatherTheme.tsx) grades whatever comes back.
 */
const LOOK =
  'Photorealistic live-action footage shot on a cinema camera with a wide lens, natural light, rich detail and ' +
  'true colour, film-like grain. One continuous locked-off wide shot with the sky filling the upper half of the ' +
  'frame. No camera movement, no cuts, no people, no animals, no text. Slow, calm motion that could go on forever.';

export const promptFor = (weather: WeatherId): string => `${SCENE[weather]} ${LOOK}`;
