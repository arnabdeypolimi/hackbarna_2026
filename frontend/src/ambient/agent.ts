import { ENDPOINT, MOCK } from './config';
import { configureFor, type SceneId } from './scenes';

/**
 * The ambient scene agent: given a scene, it opens one fal Director session, records what comes
 * back — picture and sound — and returns it as a clip. It knows nothing of React, the screen or
 * the avatar; the voice agent's command reaches it through useAmbient.ts.
 *
 * Director is a live model: it streams one continuous picture, with the sound it makes for it,
 * over WebRTC for as long as the session is open, at $0.08 a second with a 60 s minimum. A scene
 * asked for by voice is wanted for an evening, not a minute, so the stream is shown the moment it
 * arrives and recorded meanwhile; once a minute of it is kept the TV plays the clip on a loop,
 * and the next request for the scene costs nothing.
 *
 * The protocol is Director's own (fal.ai/models/minimax/h3-max/director/api): `configure` with
 * prompt_version 1 starts the stream, `stop` ends it. The browser never holds the fal key; every
 * call the SDK makes goes through the theme agent's proxy (src/tv_avatar/theme_agent), the same
 * one the weather backdrops use. This agent has the weather agent's shape on purpose — the two
 * differ in what they ask for and in recording sound — and could share one core later.
 */

export interface Progress {
  phase: 'connecting' | 'recording';
  seconds: number;
  total: number;
}

export interface Hooks {
  onProgress?(progress: Progress): void;
  /** The live picture and sound the moment fal starts sending them, so they can be watched while kept. */
  onStream?(stream: MediaStream): void;
}

export interface Made {
  blob: Blob;
  mime: string;
  seconds: number;
  /** False when no audio track ever arrived: the clip is silent, not muted. */
  sound: boolean;
}

export class AgentError extends Error {}

/** The one failure that is nobody's fault: a newer scene, or the app closing, ended the session. */
export const CANCELLED = 'Cancelled';

/** One receive slot, as the SDK's WMA transport takes it; an object tunes the Opus decode. */
type ReceiveSlot = 'video' | 'audio' | { kind: 'audio'; opus?: { stereo?: boolean; maxAverageBitrate?: number } };

/** The slice of a fal realtime session the agent uses, so dev can stand a canvas in for it. */
export interface SessionOptions {
  receive: ReceiveSlot[];
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

/** Full screen, so the picture gets what the stage can show; the same 1080p the brief asks for. */
const NEGOTIATION_TIMEOUT_MS = 90_000;
const FIRST_PICTURE_TIMEOUT_MS = 120_000;
/**
 * The picture fills the screen, so it gets more than a backdrop would: 4 Mbit/s is about 30 MB
 * a minute in VP8, which is what a set's storage can afford a few of. The sound is the brief's
 * 192 kbit/s, Director's best.
 */
const VIDEO_BITS_PER_SECOND = 4_000_000;
const AUDIO_BITS_PER_SECOND = 192_000;
/** Less than this, when the stream ends early, and it is not worth looping. */
const MIN_SECONDS = 8;
/** A frame this dark (0–255, mean of the channels) is Director's lead-in, not the scene. */
const LIT = 8;
/** But a scene that is truly this dark — a cabin at night — is not held out for forever. */
const BLACK_GRACE_MS = 20_000;
/**
 * The audio track may arrive on its own stream a moment after the video's. Recording starts
 * without it after this long, so a silent model still yields a scene rather than nothing.
 */
const SOUND_GRACE_MS = 3_000;

/**
 * VP8 with Opus first: every Chrome plays both in software whatever the set's decoders are. A TV
 * that turns out to prefer another codec is a change to this list, not to anything that uses it.
 */
const MIME_ORDER = ['video/webm;codecs=vp8,opus', 'video/webm;codecs=vp9,opus', 'video/webm'];
const MIME_ORDER_SILENT = ['video/webm;codecs=vp8', 'video/webm;codecs=vp9', 'video/webm'];

export function recordingMime(sound = true): string {
  if (typeof MediaRecorder === 'undefined' || !MediaRecorder.isTypeSupported) return '';
  return (sound ? MIME_ORDER : MIME_ORDER_SILENT).find((m) => MediaRecorder.isTypeSupported(m)) || '';
}

/** Whether this browser can make a scene at all; when it cannot, the command is refused with a reason. */
export const agentSupported = (): boolean =>
  typeof RTCPeerConnection !== 'undefined' && typeof indexedDB !== 'undefined' && recordingMime() !== '';

async function transport(proxyUrl: string): Promise<Open> {
  if (MOCK) return (await import('./mock')).mockOpen;
  // Loaded on the first request, so the SDK adds nothing to the browse screen's first paint.
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
      if (signal.aborted) return end(() => reject(new AgentError(CANCELLED)));
      if (probe.readyState >= 2 && probe.videoWidth > 0) {
        seen = seen || now;
        if (lit() || now - seen > BLACK_GRACE_MS) end(resolve);
      } else if (now - begun > FIRST_PICTURE_TIMEOUT_MS) {
        end(() => reject(new AgentError('No picture arrived from fal in time')));
      }
    }, 250);
  });
}

/** Resolves once the stream has sound, or after the grace: the scene is worth keeping either way. */
function waitForSound(stream: MediaStream, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const begun = Date.now();
    const timer = window.setInterval(() => {
      if (signal.aborted) { window.clearInterval(timer); return reject(new AgentError(CANCELLED)); }
      if (stream.getAudioTracks().length || Date.now() - begun > SOUND_GRACE_MS) { window.clearInterval(timer); resolve(); }
    }, 100);
  });
}

const messageOf = (error: unknown): string =>
  error instanceof Error && error.message ? error.message : 'fal could not be reached';

/** A browser's own words for "no answer at all", whichever engine it is. */
const NO_ANSWER = /failed to fetch|networkerror|load failed|network request failed/i;

/** A fetch that gives up rather than hanging. Chrome 84 has no AbortSignal.timeout. */
function probe(url: string): Promise<Response> {
  const abort = new AbortController();
  const timer = window.setTimeout(() => abort.abort(), 4000);
  return fetch(url, { signal: abort.signal }).finally(() => window.clearTimeout(timer));
}

/**
 * "Failed to fetch" is one message for several causes. Asked only after a failure, the proxy's
 * health route tells most of them apart; the weather README's table has the finer diagnosis.
 */
async function explain(proxyUrl: string): Promise<string> {
  const base = proxyUrl.replace(/\/fal\/proxy\/?$/, '');
  if (location.protocol === 'https:' && base.startsWith('http:')) {
    return `This page is https and the theme agent (${base}) is not, so the browser blocks it.`;
  }
  try {
    const health = await (await probe(`${base}/health`)).json();
    if (health.problem) return health.problem;
    if (!(health.sessions_left_today > 0)) return 'The theme agent has reached its daily limit of sessions. It starts over at midnight UTC.';
    return 'The theme agent is up, but the request to it failed. Its console says why.';
  } catch {
    return `The theme agent is not running at ${base}, or refused this page (${location.origin}). `
      + 'Start it: uv run uvicorn tv_avatar.theme_agent.app:create_theme_agent_app --factory --port 8010';
  }
}

export class AmbientSceneAgent {
  private running: AbortController | null = null;
  /** The in-flight create(), settled either way, so a newer one can wait for its session to close. */
  private settled: Promise<void> = Promise.resolve();

  constructor(private readonly proxyUrl: string, private readonly seconds: number) {}

  get busy(): boolean {
    return this.running !== null;
  }

  /** Ends the session now. The pending create() rejects with CANCELLED; fal still bills its 60 s minimum. */
  cancel(): void {
    this.running?.abort();
  }

  /**
   * Makes a scene. A scene asked for while another is still being made takes over: the older
   * session is closed first, so the SDK never holds two, and the older create() rejects with
   * CANCELLED rather than a failure.
   */
  async create(scene: SceneId, hooks: Hooks = {}): Promise<Made> {
    if (this.running) {
      this.running.abort();
      await this.settled;
    }
    const abort = new AbortController();
    this.running = abort;
    const run = this.run(scene, hooks, abort);
    this.settled = run.then(() => undefined, () => undefined);
    return run;
  }

  private async run(scene: SceneId, hooks: Hooks, abort: AbortController): Promise<Made> {
    // `as` because TypeScript cannot see these assigned inside the promise's callbacks.
    let session = null as SessionLike | null;
    let recorder = null as MediaRecorder | null;
    let ticker = 0;

    try {
      const open = await transport(this.proxyUrl);
      if (abort.signal.aborted) throw new AgentError(CANCELLED);
      return await new Promise<Made>((resolve, reject) => {
        const total = this.seconds;
        const chunks: Blob[] = [];
        // What is shown and recorded: fal's tracks, whichever stream each arrives on.
        const picture = new MediaStream();
        let startedAt = 0;
        let stopping = false;
        let begun = false;
        const emit = (phase: Progress['phase'], seconds: number) => hooks.onProgress?.({ phase, seconds, total });
        const fail = (message: string) => reject(new AgentError(message));

        // Ends the recording; onstop below turns what it caught into the clip, or a failure.
        const stop = () => {
          if (stopping) return;
          stopping = true;
          window.clearInterval(ticker);
          if (recorder && recorder.state !== 'inactive') recorder.stop();
          else fail('fal ended the session before a scene was made');
        };

        const record = () => {
          if (recorder) return;
          const sound = picture.getAudioTracks().length > 0;
          const mime = recordingMime(sound);
          const rec = new MediaRecorder(picture, {
            mimeType: mime,
            videoBitsPerSecond: VIDEO_BITS_PER_SECOND,
            audioBitsPerSecond: AUDIO_BITS_PER_SECOND,
          });
          recorder = rec;
          rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
          rec.onerror = () => fail('Recording failed');
          rec.onstop = () => {
            const seconds = Math.round((Date.now() - startedAt) / 1000);
            // Relative to what was asked for, so a clip that ran its whole (short) length is never too short.
            if (seconds < Math.min(MIN_SECONDS, total * 0.75) || !chunks.length) return fail('The stream ended before there was enough to keep');
            const type = rec.mimeType || mime;
            resolve({ blob: new Blob(chunks, { type }), mime: type, seconds, sound });
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

        // Once there is a picture the viewer sees it; the recording waits for it to show
        // something, then a moment for the sound, so no loop starts on black or in silence.
        const begin = () => {
          if (begun || !picture.getVideoTracks().length) return;
          begun = true;
          hooks.onStream?.(picture);
          waitForPicture(picture, abort.signal)
            .then(() => waitForSound(picture, abort.signal))
            .then(record)
            .catch((e) => fail(messageOf(e)));
        };
        // The SDK hands over each stream once, and a server that names no stream gets one per
        // track: the audio may come on its own stream, or on this one a moment later.
        const adopt = (stream: MediaStream) => {
          for (const t of stream.getTracks()) {
            if (!picture.getTracks().some((p) => p.id === t.id)) picture.addTrack(t);
          }
          begin();
        };

        emit('connecting', 0);
        session = open({
          receive: ['video', { kind: 'audio', opus: { stereo: true, maxAverageBitrate: AUDIO_BITS_PER_SECOND } }],
          negotiationTimeoutMs: NEGOTIATION_TIMEOUT_MS,
          abortSignal: abort.signal,
          onMedia: (stream) => {
            adopt(stream);
            stream.addEventListener('addtrack', () => adopt(stream));
          },
          onData: (raw) => {
            if (import.meta.env.DEV) console.debug('[ambient] fal:', raw);
            let message: { type?: string; code?: string } = {};
            try { message = JSON.parse(raw); } catch { return; }
            if (message.type === 'error') fail(`fal reported an error${message.code ? ` (${message.code})` : ''}`);
            // The model has nothing more to give: keep what was caught, if it is enough.
            else if (message.type === 'stream_exhausted') stop();
            // configured, prompt_*, audio_*, chunk, chunk_metrics: progress, nothing to do.
          },
          onState: (state) => {
            if (state !== 'closed' || stopping) return;
            if (abort.signal.aborted) fail(CANCELLED);
            else stop();
          },
          onError: (error) => fail(messageOf(error)),
        });

        session.send(configureFor(scene, total));
      });
    } catch (e) {
      if (!MOCK && e instanceof AgentError && NO_ANSWER.test(e.message)) {
        const why = await explain(this.proxyUrl);
        console.warn('[ambient]', e.message, '-', why);
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
      if (this.running === abort) this.running = null;
    }
  }
}
