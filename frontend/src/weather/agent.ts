import type { ThemeId } from '../lib/theme';
import { promptFor, type WeatherId } from './catalogue';
import { ENDPOINT, MOCK } from './config';

/**
 * The weather agent: given a season and a weather, it opens one fal Director session, records
 * what comes back, and returns it as a clip. It knows nothing of React, the room or the picker.
 *
 * Director is a live model: it streams one continuous picture over WebRTC for as long as the
 * session is open, steered by text, at $0.08 a second with a 60 s minimum. It cannot be a
 * background that stays generating, and it does not need to: a backdrop is the same footage
 * every time, so one session is recorded once, kept, and played as a plain looping video —
 * the same single hardware-decoded <video> the season's own loop is.
 *
 * The protocol is Director's own (fal.ai/models/minimax/h3-max/director/api): `configure` with
 * prompt_version 1 starts the stream, `stop` ends it. The browser never holds the fal key;
 * every call the SDK makes goes through the theme agent's proxy (src/tv_avatar/theme_agent).
 */

export interface Job {
  season: ThemeId;
  weather: WeatherId;
}

export interface Progress {
  phase: 'connecting' | 'recording';
  seconds: number;
  total: number;
}

export interface Hooks {
  onProgress?(progress: Progress): void;
  /** The live picture the moment fal starts sending it, so it can be watched while it is kept. */
  onStream?(stream: MediaStream): void;
}

export interface Made {
  blob: Blob;
  mime: string;
  seconds: number;
}

export class AgentError extends Error {}

/** The slice of a fal realtime session the agent uses, so dev can stand a canvas in for it. */
export interface SessionOptions {
  receive: ('video' | 'audio')[];
  negotiationTimeoutMs: number;
  abortSignal: AbortSignal;
  onMedia(stream: MediaStream): void;
  onData(raw: string): void;
  onState(state: string): void;
  onError(error: unknown): void;
}

export interface SessionLike {
  send(message: object): void;
  close(): Promise<void> | void;
}

export type Open = (options: SessionOptions) => SessionLike;

/**
 * Locked for a session, and the cheapest that is still sharp behind a scrim: the picture is
 * dimmed and grained before it is seen, and the stage is only ever 1080p at most.
 */
const RESOLUTION = '1080p';
const ASPECT = '16:9';
/** A soft ambient picture needs far less than the default; 1.8 Mbit/s keeps a clip near 9 MB a minute. */
const BITS_PER_SECOND = 1_800_000;
/** Less than this, when the stream ends early, and it is not worth looping. */
const MIN_SECONDS = 8;
/** Fal may wait for a runner; without a deadline a full pool would leave the viewer waiting forever. */
const NEGOTIATION_TIMEOUT_MS = 90_000;
const FIRST_PICTURE_TIMEOUT_MS = 120_000;
/** A frame this dark (0–255, mean of the channels) is Director's lead-in, not the scene. */
const LIT = 8;
/** But a scene that is truly this dark is not held out for forever. */
const BLACK_GRACE_MS = 20_000;

/**
 * VP8 first: every Chrome plays it in software whatever the set's decoders are. A TV that
 * turns out to prefer another codec is a change to this list, not to anything that uses it.
 */
const MIME_ORDER = ['video/webm;codecs=vp8', 'video/webm;codecs=vp9', 'video/webm'];

export function recordingMime(): string {
  if (typeof MediaRecorder === 'undefined' || !MediaRecorder.isTypeSupported) return '';
  return MIME_ORDER.find((m) => MediaRecorder.isTypeSupported(m)) || '';
}

/** Whether this browser can make a backdrop at all; when it cannot, the picker offers none. */
export const agentSupported = (): boolean =>
  typeof RTCPeerConnection !== 'undefined' && typeof indexedDB !== 'undefined' && recordingMime() !== '';

async function transport(proxyUrl: string): Promise<Open> {
  if (MOCK) return (await import('./mock')).mockOpen;
  // Loaded on the first request, so the SDK and its msgpack dependency add nothing to the
  // browse screen's first paint on a 1 GB set.
  const [{ createFalClient }, { wma }] = await Promise.all([
    import('@fal-ai/client'),
    import('@fal-ai/client/realtime'),
  ]);
  const fal = createFalClient({ proxyUrl });
  return (options) => fal.realtime.open(wma(ENDPOINT), options);
}

/**
 * Resolves at the first frame that shows something. A remote track has no picture until its
 * first packets, and Director may lead with black while its first chunk is made; recording
 * that would put a black second at the start of every loop.
 */
function waitForPicture(stream: MediaStream, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const probe = document.createElement('video');
    probe.muted = true;
    probe.playsInline = true;
    probe.srcObject = stream;
    probe.play().catch(() => { /* muted, so this is only a stream that has not started */ });
    const canvas = document.createElement('canvas');
    canvas.width = 16;
    canvas.height = 9;
    const ctx = canvas.getContext('2d');

    const lit = () => {
      if (!ctx) return true;
      try {
        ctx.drawImage(probe, 0, 0, 16, 9);
        const px = ctx.getImageData(0, 0, 16, 9).data;
        let sum = 0;
        for (let i = 0; i < px.length; i += 4) sum += px[i] + px[i + 1] + px[i + 2];
        return sum / (16 * 9 * 3) > LIT;
      } catch {
        return true; // cannot read it back: take the first frame rather than never start
      }
    };

    const begun = Date.now();
    let seen = 0;
    const end = (settle: () => void) => { window.clearInterval(timer); probe.srcObject = null; settle(); };
    const timer = window.setInterval(() => {
      const now = Date.now();
      if (signal.aborted) return end(() => reject(new AgentError('Cancelled')));
      if (probe.readyState >= 2 && probe.videoWidth > 0) {
        seen = seen || now;
        if (lit() || now - seen > BLACK_GRACE_MS) end(resolve);
      } else if (now - begun > FIRST_PICTURE_TIMEOUT_MS) {
        end(() => reject(new AgentError('No picture arrived from fal in time')));
      }
    }, 250);
  });
}

const messageOf = (error: unknown): string =>
  error instanceof Error && error.message ? error.message : 'fal could not be reached';

/** A browser's own words for "no answer at all", whichever engine it is. */
const NO_ANSWER = /failed to fetch|networkerror|load failed|network request failed/i;

/** A fetch that gives up rather than hanging, for the diagnosis below. Chrome 84 has no AbortSignal.timeout. */
function probe(url: string, init: RequestInit = {}): Promise<Response> {
  const abort = new AbortController();
  const timer = window.setTimeout(() => abort.abort(), 4000);
  return fetch(url, { ...init, signal: abort.signal }).finally(() => window.clearTimeout(timer));
}

/**
 * "Failed to fetch" is one message for four different causes: the agent is not running, it
 * refused this page's origin, the page is https and the agent is not, or the network is down.
 * Told as it arrives it sends the viewer nowhere, so this asks the agent's health route which
 * one it is, and only after a failure: a press that works costs no extra request.
 */
async function explain(proxyUrl: string): Promise<string> {
  const base = proxyUrl.replace(/\/fal\/proxy\/?$/, '');
  if (location.protocol === 'https:' && base.startsWith('http:')) {
    return `This page is https and the theme agent (${base}) is not, so the browser blocks it. Serve the agent over https and set VITE_WEATHER_PROXY_URL.`;
  }
  try {
    const health = await (await probe(`${base}/health`)).json();
    // No key, or one with characters it cannot carry: the agent says which, in words for a person.
    if (health.problem) return health.problem;
    if (!(health.sessions_left_today > 0)) return 'The theme agent has reached its daily limit of backdrops. It starts over at midnight UTC.';
    return 'The theme agent is up, but the request to it failed. Its console says why.';
  } catch {
    // Does anything answer at all? A no-cors request resolves for any server that does, and
    // rejects only when nothing does, which is what tells "refused" from "not running".
    try {
      await probe(`${base}/health`, { mode: 'no-cors' });
      return `The theme agent refused this page (${location.origin}). Add that origin to THEME_AGENT_ORIGINS in .env and restart it.`;
    } catch {
      return `The theme agent is not running at ${base}. Start it: uv run uvicorn tv_avatar.theme_agent.app:create_theme_agent_app --factory --port 8010`;
    }
  }
}

export class WeatherThemeAgent {
  private running: AbortController | null = null;

  constructor(private readonly proxyUrl: string, private readonly seconds: number) {}

  get busy(): boolean {
    return this.running !== null;
  }

  /** Ends the session now. The pending create() rejects; fal still bills its 60 s minimum. */
  cancel(): void {
    this.running?.abort();
  }

  async create(job: Job, hooks: Hooks = {}): Promise<Made> {
    if (this.running) throw new AgentError('Another backdrop is still being created');
    const abort = new AbortController();
    this.running = abort;
    // `as` because TypeScript cannot see these assigned inside the promise's callbacks.
    let session = null as SessionLike | null;
    let recorder = null as MediaRecorder | null;
    let ticker = 0;

    try {
      const open = await transport(this.proxyUrl);
      return await new Promise<Made>((resolve, reject) => {
        const total = this.seconds;
        const chunks: Blob[] = [];
        let startedAt = 0;
        let stopping = false;
        const emit = (phase: Progress['phase'], seconds: number) => hooks.onProgress?.({ phase, seconds, total });
        const fail = (message: string) => reject(new AgentError(message));

        // Ends the recording; onstop below turns what it caught into the clip, or a failure.
        const stop = () => {
          if (stopping) return;
          stopping = true;
          window.clearInterval(ticker);
          if (recorder && recorder.state !== 'inactive') recorder.stop();
          else fail('fal ended the session before a backdrop was made');
        };

        const record = (stream: MediaStream) => {
          if (recorder) return;
          const mime = recordingMime();
          // Video only: the backdrop is silent, and the audio would be bytes for nothing.
          const rec = new MediaRecorder(new MediaStream(stream.getVideoTracks()), {
            mimeType: mime,
            videoBitsPerSecond: BITS_PER_SECOND,
          });
          recorder = rec;
          rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
          rec.onerror = () => fail('Recording failed');
          rec.onstop = () => {
            const seconds = Math.round((Date.now() - startedAt) / 1000);
            // Relative to what was asked for, so a clip that ran its whole (short) length is never too short.
            if (seconds < Math.min(MIN_SECONDS, total * 0.75) || !chunks.length) return fail('The stream ended before there was enough to keep');
            const type = rec.mimeType || mime;
            resolve({ blob: new Blob(chunks, { type }), mime: type, seconds });
          };
          startedAt = Date.now();
          rec.start(1000);
          emit('recording', 0);
          ticker = window.setInterval(() => {
            const elapsed = (Date.now() - startedAt) / 1000;
            emit('recording', Math.min(total, Math.floor(elapsed)));
            if (elapsed >= total) stop();
          }, 500);
        };

        emit('connecting', 0);
        session = open({
          receive: ['video', 'audio'],
          negotiationTimeoutMs: NEGOTIATION_TIMEOUT_MS,
          abortSignal: abort.signal,
          onMedia: (stream) => {
            // The SDK hands over each stream once, and a server that names no stream gets one
            // per track. The audio-only one is not the picture; the video one may be this same
            // stream a moment later.
            const begin = () => {
              if (!stream.getVideoTracks().length) return false;
              hooks.onStream?.(stream);
              waitForPicture(stream, abort.signal).then(() => record(stream)).catch((e) => fail(messageOf(e)));
              return true;
            };
            if (begin()) return;
            const onAdd = () => { if (begin()) stream.removeEventListener('addtrack', onAdd); };
            stream.addEventListener('addtrack', onAdd);
          },
          onData: (raw) => {
            if (import.meta.env.DEV) console.debug('[weather] fal:', raw);
            let message: { type?: string; code?: string } = {};
            try { message = JSON.parse(raw); } catch { return; }
            if (message.type === 'error') fail(`fal reported an error${message.code ? ` (${message.code})` : ''}`);
            // The model has nothing more to give: keep what was caught, if it is enough.
            else if (message.type === 'stream_exhausted') stop();
            // configured, prompt_*, chunk, chunk_metrics, deadline_missed: progress, nothing to do.
          },
          onState: (state) => {
            if (state !== 'closed' || stopping) return;
            if (abort.signal.aborted) fail('Cancelled');
            else stop();
          },
          onError: (error) => fail(messageOf(error)),
        });

        session.send({
          type: 'configure',
          protocol_version: 1,
          prompt_version: 1,
          resolution: RESOLUTION,
          aspect_ratio: ASPECT,
          memory: 3,
          prompt: promptFor(job.weather),
        });
      });
    } catch (e) {
      if (!MOCK && e instanceof AgentError && NO_ANSWER.test(e.message)) {
        const why = await explain(this.proxyUrl);
        console.warn('[weather]', e.message, '-', why);
        throw new AgentError(why);
      }
      throw e;
    } finally {
      window.clearInterval(ticker);
      if (recorder && recorder.state !== 'inactive') {
        try { recorder.stop(); } catch { /* already stopping */ }
      }
      try { session?.send({ type: 'stop' }); } catch { /* the session is already gone */ }
      // Closing is what ends fal's billing, so it happens on every path, and is awaited.
      abort.abort();
      await Promise.resolve(session?.close()).catch(() => undefined);
      this.running = null;
    }
  }
}
