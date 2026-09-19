import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Titan OS TVs from 2020–2022 run Chrome 84, so compile JS and CSS down to that.
export default defineConfig({
  base: './',
  plugins: [react()],
  build: { target: 'chrome84', cssTarget: 'chrome84' },
  server: { host: true },
});
