# Dataset goes here

Put your titles file in this folder as **`titles.csv`**. The app loads it on startup from `data/titles.csv`.

To make it from the TMDB export (`TMDB_movie_dataset_v11.csv`):

```bash
npm run trim -- path/to/TMDB_movie_dataset_v11.csv public/data/titles.csv 600
```

Until the file is here, the app shows a "No titles yet" screen. You can still load a CSV for testing with the Import CSV button (or the yellow key).

To load the data from a server instead, change `DATA_URL` in `src/config.ts`.
