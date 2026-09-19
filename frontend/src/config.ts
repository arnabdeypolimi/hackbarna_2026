// Where the dataset is loaded from. Drop titles.csv into public/data/, or point this at a server URL.
export const DATA_URL = `${import.meta.env.BASE_URL}data/titles.csv`;

// Poster paths like "/abc.jpg" (TMDB style) are prefixed with this.
export const TMDB_IMAGE_BASE = 'https://image.tmdb.org/t/p/';
export const POSTER_SIZE = 'w342';
export const BACKDROP_SIZE = 'w780';

export const ROW_MAX = 30; // posters per row; keeps the DOM small on 1–1.5 GB TVs
export const MAX_TITLES = 2000; // larger files are cut to the most popular titles
export const MAX_FILE_BYTES = 50_000_000;
