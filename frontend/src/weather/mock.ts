import type { SessionLike, SessionOptions } from './agent';

/**
 * A stand-in for a Director session, for `?weatherMock=1`: a canvas that draws the weather the
 * prompt asks for and streams it like a remote track would arrive. It speaks the same messages,
 * so everything downstream of the session — waiting for a picture, recording, storing, looping —
 * runs the real code with no key and no bill. Loaded only on that flag.
 */

const W = 640;
const H = 360;

type Kind = 'clear' | 'cloudy' | 'rain' | 'storm' | 'snow' | 'fog';

/** The sky each weather draws; the prompt names no season, so the weather sets the palette. */
const SKY: Record<Kind, [string, string]> = {
  clear: ['#6699e6', '#e6f2ff'],
  cloudy: ['#7a8089', '#c9ccd1'],
  rain: ['#5a6470', '#aeb6bf'],
  storm: ['#2f343c', '#6b7079'],
  snow: ['#8f9aa8', '#eef2f6'],
  fog: ['#a9b0b8', '#e2e6ea'],
};

/**
 * Reads the scene back out of the prompt catalogue.ts wrote: its wording is what varies. Whole
 * words, because every prompt asks for film "grain"; and storm before rain, because a storm
 * has rain in it.
 */
function sceneOf(prompt: string): { kind: Kind; top: string; bottom: string } {
  const has = (word: string) => new RegExp(`\\b${word}\\b`, 'i').test(prompt);
  const kind: Kind = has('storm') ? 'storm'
    : has('snowfall') ? 'snow'
    : has('overcast') ? 'cloudy'
    : has('rain') ? 'rain'
    : has('fog') ? 'fog'
    : 'clear';
  const [top, bottom] = SKY[kind];
  return { kind, top, bottom };
}

export function mockOpen(options: SessionOptions): SessionLike {
  const canvas = document.createElement('canvas');
  canvas.width = W;
  canvas.height = H;
  const ctx = canvas.getContext('2d') as CanvasRenderingContext2D;
  const specks = Array.from({ length: 160 }, () => ({ x: Math.random() * W, y: Math.random() * H, v: 1 + Math.random() * 3 }));
  let scene = sceneOf('');
  let frame = 0;
  let timer = 0;
  let start = 0;
  let closed = false;
  let stream: MediaStream | null = null;

  const draw = () => {
    frame += 1;
    const sky = ctx.createLinearGradient(0, 0, 0, H);
    sky.addColorStop(0, scene.top);
    sky.addColorStop(1, scene.bottom);
    ctx.fillStyle = sky;
    ctx.fillRect(0, 0, W, H);

    const { kind } = scene;
    ctx.fillStyle = kind === 'storm' || kind === 'cloudy' ? 'rgba(40, 44, 52, 0.45)' : 'rgba(255, 255, 255, 0.35)';
    for (let i = 0; i < 4; i++) {
      const x = ((frame * (0.4 + i * 0.15) + i * 190) % (W + 300)) - 150;
      ctx.beginPath();
      ctx.ellipse(x, 70 + i * 32, 130, 34, 0, 0, Math.PI * 2);
      ctx.fill();
    }
    if (kind === 'rain' || kind === 'storm') {
      ctx.strokeStyle = 'rgba(210, 225, 245, 0.55)';
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      for (const s of specks) {
        s.y = (s.y + s.v * 6) % H;
        ctx.moveTo(s.x, s.y);
        ctx.lineTo(s.x - 3, s.y + 16);
      }
      ctx.stroke();
    }
    if (kind === 'snow') {
      ctx.fillStyle = 'rgba(255, 255, 255, 0.9)';
      for (const s of specks) {
        s.y = (s.y + s.v * 0.7) % H;
        ctx.fillRect((s.x + Math.sin((frame + s.y) / 30) * 8) % W, s.y, 3, 3);
      }
    }
    if (kind === 'fog') {
      ctx.fillStyle = 'rgba(235, 240, 245, 0.55)';
      for (let i = 0; i < 3; i++) ctx.fillRect(((frame * (0.6 + i * 0.3)) % W) - W, 120 + i * 70, W * 2, 60);
    }
    if (kind === 'storm' && frame % 110 < 3) {
      ctx.fillStyle = 'rgba(255, 255, 255, 0.5)';
      ctx.fillRect(0, 0, W, H);
    }
  };

  return {
    send(message) {
      const m = message as { type?: string; prompt?: string };
      if (m.type !== 'configure') return;
      scene = sceneOf(m.prompt || '');
      start = window.setTimeout(() => {
        if (closed) return;
        options.onData(JSON.stringify({ type: 'configured', prompt_version: 1 }));
        draw();
        timer = window.setInterval(draw, 1000 / 24);
        stream = canvas.captureStream(24);
        options.onMedia(stream);
      }, 400);
    },
    close() {
      closed = true;
      window.clearTimeout(start);
      window.clearInterval(timer);
      stream?.getTracks().forEach((t) => t.stop());
    },
  };
}
