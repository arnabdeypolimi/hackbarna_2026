import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Titan OS TVs from 2020–2022 run Chrome 84, so compile JS and CSS down to that.
export default defineConfig({
  base: './',
  plugins: [react()],
  build: { target: 'chrome84', cssTarget: 'chrome84' },
  server: {
    host: true,
    // The avatar backend runs separately (uvicorn on :8000). Proxying keeps the
    // app same-origin, which is what lets the control socket and the WebRTC
    // signalling POST share one host with no CORS on the Python side.
    proxy: {
      '/config': 'http://localhost:8000',
      // ws: true matters — the control channel is a WebSocket under the same
      // /sessions prefix as the HTTP signalling, so one entry covers both.
      '/sessions': { target: 'http://localhost:8000', ws: true },
    },
  },
});
