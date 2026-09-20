import type { SessionLike, SessionOptions } from './agent';

/**
 * A stand-in for a Director session, for `?ambientMock=1`: a canvas that draws the scene the
 * prompt asks for, with a low tone for its sound, streamed the way remote tracks would arrive.
 * It speaks the same messages, so everything downstream of the session — waiting for a picture
 * and for sound, recording both, keeping, looping, the fullscreen chrome and closing — runs the
 * real code with no key and no bill. Loaded only on that flag.
 */

const W = 640;
const H = 360;

type Kind = 'fire' | 'sea' | 'garden';

/** Reads the scene back out of the prompt scenes.ts wrote: the words that only one of them uses. */
const kindOf = (prompt: string): Kind => {
  const p = prompt.toLowerCase();
  return /fireplace|cabin|embers/.test(p) ? 'fire' : /ocean|beach|shoreline/.test(p) ? 'sea' : 'garden';
};

/** A tone per scene, low and quiet: enough to hear that sound is recorded and which scene it is. */
const TONE: Record<Kind, number> = { fire: 110, sea: 165, garden: 330 };

export function mockOpen(options: SessionOptions): SessionLike {
  const canvas = document.createElement('canvas');
  canvas.width = W;
  canvas.height = H;
  const ctx = canvas.getContext('2d') as CanvasRenderingContext2D;
  const specks = Array.from({ length: 120 }, () => ({ x: Math.random() * W, y: Math.random() * H, v: 0.5 + Math.random() * 2 }));
  let kind: Kind = 'garden';
  let frame = 0;
  let timer = 0;
  let start = 0;
  let closed = false;
  let stream: MediaStream | null = null;
  let audio: AudioContext | null = null;

  const fill = (top: string, bottom: string, y0 = 0, y1 = H) => {
    const g = ctx.createLinearGradient(0, y0, 0, y1);
    g.addColorStop(0, top);
    g.addColorStop(1, bottom);
    ctx.fillStyle = g;
    ctx.fillRect(0, y0, W, y1 - y0);
  };

  const draw = () => {
    frame += 1;
    if (kind === 'fire') {
      fill('#2a1608', '#050302');
      ctx.fillStyle = '#3a2a22';
      ctx.fillRect(W * 0.3, H * 0.45, W * 0.4, H * 0.45); // the hearth
      for (let i = 0; i < 6; i++) {
        const flicker = Math.sin(frame * 0.35 + i * 1.7) * 12;
        ctx.fillStyle = i % 2 ? 'rgba(255, 140, 40, 0.75)' : 'rgba(255, 210, 90, 0.7)';
        ctx.beginPath();
        ctx.ellipse(W * 0.4 + i * 26, H * 0.78 + flicker * 0.3, 18 + (i % 3) * 6, 46 + flicker, 0, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.fillStyle = 'rgba(255, 200, 120, 0.9)';
      for (const s of specks.slice(0, 30)) {
        s.y = (s.y - s.v) % H;
        if (s.y < 0) s.y += H;
        ctx.fillRect(W * 0.35 + (s.x % (W * 0.3)), s.y, 2, 2);
      }
    } else if (kind === 'sea') {
      fill('#7fc4ff', '#e8f6ff', 0, H * 0.5);
      fill('#1f8fc7', '#7fd4e8', H * 0.5, H);
      ctx.strokeStyle = 'rgba(255, 255, 255, 0.7)';
      ctx.lineWidth = 3;
      for (let i = 0; i < 3; i++) {
        ctx.beginPath();
        for (let x = 0; x <= W; x += 8) {
          const y = H * (0.6 + i * 0.12) + Math.sin(x / 40 + frame / 12 + i) * 6;
          if (x === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        }
        ctx.stroke();
      }
    } else {
      fill('#ffd9e6', '#dff5d9');
      ctx.fillStyle = 'rgba(255, 245, 200, 0.6)';
      ctx.beginPath();
      ctx.ellipse(W * 0.75, H * 0.25, 90, 90, 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = 'rgba(240, 130, 170, 0.8)';
      for (const s of specks) {
        s.y = (s.y + s.v * 0.4) % H;
        ctx.fillRect((s.x + Math.sin((frame + s.y) / 40) * 10) % W, s.y, 4, 4);
      }
    }
  };

  /** The tone, on a track of its own, as fal's audio would be. Silent until a gesture resumes the context. */
  const sound = (): MediaStreamTrack | null => {
    try {
      audio = new AudioContext();
      const osc = audio.createOscillator();
      osc.type = 'triangle';
      osc.frequency.value = TONE[kind];
      const gain = audio.createGain();
      gain.gain.value = 0.06;
      const dest = audio.createMediaStreamDestination();
      osc.connect(gain).connect(dest);
      osc.start();
      audio.resume().catch(() => undefined);
      return dest.stream.getAudioTracks()[0] || null;
    } catch {
      return null;
    }
  };

  return {
    send(message) {
      const m = message as { type?: string; prompt?: string };
      if (m.type !== 'configure') return;
      kind = kindOf(m.prompt || '');
      start = window.setTimeout(() => {
        if (closed) return;
        options.onData(JSON.stringify({ type: 'configured', prompt_version: 1 }));
        draw();
        timer = window.setInterval(draw, 1000 / 24);
        stream = canvas.captureStream(24);
        const track = sound();
        if (track) stream.addTrack(track);
        options.onMedia(stream);
      }, 400);
    },
    close() {
      closed = true;
      window.clearTimeout(start);
      window.clearInterval(timer);
      stream?.getTracks().forEach((t) => t.stop());
      audio?.close().catch(() => undefined);
    },
  };
}
