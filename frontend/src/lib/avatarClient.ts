// One conversation with the avatar backend: a WebRTC peer connection carrying the
// microphone up and the avatar's audio and video down, and a WebSocket carrying
// status, transcript and commands down, and screen state, acks and results up.
// Deliberately framework-free.
import type {
  AgentStatusMsg, ClientMessage, CommandMsg, ServerMessage, TranscriptMsg,
} from '@contracts/protocol';

export type AgentState = AgentStatusMsg['state'];
/**
 * A client message before the version is stamped; `send()` adds `v`. Distributive on
 * purpose: a plain `Omit` over the union would keep only the keys every member shares.
 */
export type Outbound = ClientMessage extends infer M ? (M extends ClientMessage ? Omit<M, 'v'> : never) : never;

// Hand-written because tools/export_schemas.py emits the wire protocol only:
// /config and POST /sessions are ad-hoc dicts in app.py rather than Pydantic
// models, so the generator never sees them. Promoting them is a follow-up —
// see docs/superpowers/specs/2026-09-19-frontend-avatar-panel-design.md.
export interface AvatarInfo {
  id: string;
  name: string;
  description: string;
  avatar_model: string;
  languages: string[];
}

export interface LanguageInfo {
  code: string;
  name: string;
  native_name: string;
}

export interface BackendConfig {
  configured: boolean;
  missing: string[];
  llm_model: string;
  stt_model: string;
  tts_model: string;
  tts_sample_rate: number;
  default_avatar: string;
  default_language: string;
  avatars: AvatarInfo[];
  languages: LanguageInfo[];
}

interface SessionInfo {
  session_id: string;
  avatar: string;
  language: string;
  control_token: string;
  control_url: string;
  offer_url: string;
  protocol_version: number;
}

export interface AvatarSession {
  sessionId: string;
  avatar: string;
  language: string;
  /** The avatar's audio and video. Attaching it is the caller's job — see ConnectOptions. */
  stream: MediaStream;
  /** Client→server half of the protocol. A no-op on a socket that is not open. */
  send(msg: Outbound): void;
  close(): void;
}

// No video element here, on purpose. Two rapid chip presses run two connect()s against the
// same element; if the loser attached its stream after the winner it would overwrite the
// winner's, and then have its own tracks stopped when the caller discards it — a live phase
// with a black face and no message. The caller attaches the stream only after it has decided
// the attempt is still the current one.
export interface ConnectOptions {
  avatar: string;
  language: string;
  /** The viewer's profile id. History and memory are keyed by it on the backend (D10). */
  userId?: string;
  /**
   * Whether to ask for the talking head at all. False builds the pipeline without the
   * avatar — the backend's own `avatar` query flag — so nothing generates a frame and no
   * Anam session is minted. The voice is unaffected. Because it changes how the pipeline
   * is built, changing it means a new session, not a change to a running one.
   */
  video: boolean;
  onStatus(state: AgentState): void;
  onTranscript(msg: TranscriptMsg): void;
  onCommand(msg: CommandMsg): void;
  onError(err: Error): void;
}

interface Parts {
  mic?: MediaStream;
  ws?: WebSocket;
  pc?: RTCPeerConnection;
  session?: SessionInfo;
  // A teardown we asked for is not a disconnection worth reporting: close() closes the
  // socket and the peer connection itself, and both announce the loss on their way out.
  closing: boolean;
}

const MIC: MediaTrackConstraints = {
  echoCancellation: true,
  noiseSuppression: true,
  autoGainControl: true,
};

export async function fetchConfig(): Promise<BackendConfig> {
  const res = await fetch('/config');
  if (!res.ok) throw new Error(`GET /config failed (${res.status})`);
  return (await res.json()) as BackendConfig;
}

export async function connect(opts: ConnectOptions): Promise<AvatarSession> {
  const parts: Parts = { closing: false };

  const close = () => {
    parts.closing = true;
    parts.pc?.close();
    parts.ws?.close();
    parts.mic?.getTracks().forEach((t) => t.stop());
    const s = parts.session;
    if (!s) return;
    // Fire and forget: the backend sweeps sessions the browser fails to close.
    fetch(`/sessions/${s.session_id}`, {
      method: 'DELETE',
      headers: { 'X-Control-Token': s.control_token },
      // This also runs from `beforeunload`, where an ordinary fetch is cancelled along with
      // the page. sendBeacon cannot carry X-Control-Token; keepalive can, and is Chrome 66.
      keepalive: true,
    }).catch(() => {});
  };

  // The protocol version is stamped here and nowhere else. Dropping on a closed socket is
  // correct: screen state is latest-wins and re-sent on change, and a result for a command
  // the backend has already timed out is of no use to it.
  const send = (msg: Outbound) => {
    const ws = parts.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ v: 1, ...msg }));
  };

  try {
    // The microphone first. A refusal here is the common failure and it must not
    // leave a half-built session sitting on the backend.
    parts.mic = await navigator.mediaDevices.getUserMedia({ audio: MIC });
    parts.session = await createSession(opts.avatar, opts.language, opts.userId);
    parts.ws = openControl(parts.session, opts, parts);
    const media = await openMedia(parts.session, parts.mic, opts, parts);
    parts.pc = media.pc;
    return {
      sessionId: parts.session.session_id,
      avatar: parts.session.avatar,
      language: parts.session.language,
      stream: media.stream,
      send,
      close,
    };
  } catch (err) {
    close();
    throw err;
  }
}

async function createSession(avatar: string, language: string, userId?: string): Promise<SessionInfo> {
  const res = await fetch('/sessions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ avatar, language, user_id: userId }),
  });
  if (!res.ok) throw new Error(`POST /sessions failed (${res.status})`);
  const info = (await res.json()) as SessionInfo;
  // Mirrors PROTOCOL_VERSION in contracts/protocol.d.ts, which is a `.d.ts` const and so has
  // no runtime value to import. The TV app and the backend ship separately; a version we do
  // not speak is an error, not something to parse optimistically.
  if (info.protocol_version !== 1) {
    throw new Error(`backend speaks protocol v${info.protocol_version}, this app speaks v1`);
  }
  return info;
}

function openControl(session: SessionInfo, opts: ConnectOptions, parts: Parts): WebSocket {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(
    `${proto}://${location.host}${session.control_url}?token=${session.control_token}`,
  );
  ws.onmessage = (ev) => {
    let msg: ServerMessage;
    try {
      msg = JSON.parse(ev.data as string) as ServerMessage;
    } catch {
      // A frame we cannot read is a protocol failure the viewer has to be told about, not
      // a throw inside an event handler where nothing is listening for it.
      opts.onError(new Error('unreadable control frame'));
      return;
    }
    switch (msg.type) {
      case 'agent_status':
        opts.onStatus(msg.state);
        break;
      case 'transcript':
        opts.onTranscript(msg);
        break;
      case 'command':
        opts.onCommand(msg);
        break;
      case 'error':
        // The backend sends this when a message *we* sent was malformed. The session
        // itself is fine — tearing it down would turn one bad ack into a dead avatar.
        console.warn('[avatar] protocol error', msg.code, msg.message);
        break;
    }
  };
  ws.onerror = () => opts.onError(new Error('control socket failed'));
  // The mirror of the rule that every non-live phase says what happened: a live phase that
  // is dead must not stay silent. A backend restart, a network blip or the session's TTL
  // being reaped would otherwise leave a frozen face and a status pill that lies.
  ws.onclose = () => { if (!parts.closing) opts.onError(new Error('control connection lost')); };
  return ws;
}

async function openMedia(
  session: SessionInfo,
  mic: MediaStream,
  opts: ConnectOptions,
  parts: Parts,
): Promise<{ pc: RTCPeerConnection; stream: MediaStream }> {
  const pc = new RTCPeerConnection();
  // Owned here until it is handed back. A throw between construction and the return
  // would otherwise strand it: the caller's cleanup reads a variable this function
  // never got to assign, and a peer connection is not reclaimed by losing its last
  // reference — its ICE agent and DTLS state stay alive. The backend answering a
  // misconfigured offer with 503 is the everyday way to hit that.
  try {
    mic.getTracks().forEach((t) => pc.addTrack(t, mic));
    // Offered only when the head is wanted: with avatar=false below there is nothing on the
    // other end to fill it, and an m-line for a track the backend never produces is noise.
    if (opts.video) pc.addTransceiver('video', { direction: 'recvonly' });

    const stream = new MediaStream();
    pc.ontrack = (ev) => { stream.addTrack(ev.track); };

    // The media half of the same rule as ws.onclose above: without it the face simply
    // freezes. `disconnected` is deliberately not in this list — ICE enters it on any brief
    // blip and recovers on its own, and Chrome promotes a connection that really died to
    // `failed` once consent freshness expires. Reporting `disconnected` would tear down a
    // healthy paid session for a two-second hiccup. tools/demo/demo.js draws the same line.
    pc.oniceconnectionstatechange = () => {
      const s = pc.iceConnectionState;
      if (parts.closing) return;
      if (s === 'failed' || s === 'closed') {
        opts.onError(new Error('media connection lost'));
      }
    };

    await pc.setLocalDescription(await pc.createOffer());
    await iceGathered(pc);

    const url = `${session.offer_url}?token=${session.control_token}&avatar=${opts.video}&halfduplex=false`;
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sdp: pc.localDescription?.sdp, type: pc.localDescription?.type }),
    });
    if (!res.ok) throw new Error(`WebRTC offer rejected (${res.status})`);
    const answer = (await res.json()) as { sdp: string; type: RTCSdpType };
    await pc.setRemoteDescription(answer);
    return { pc, stream };
  } catch (err) {
    pc.close();
    throw err;
  }
}

// Vanilla ICE rather than trickle: one round trip, and nothing on a LAN needs more.
// The 2 s cap is the give-up, because a gathering that stalls must not hang the panel.
function iceGathered(pc: RTCPeerConnection): Promise<void> {
  return new Promise((resolve) => {
    if (pc.iceGatheringState === 'complete') return resolve();
    const done = () => {
      pc.removeEventListener('icegatheringstatechange', check);
      resolve();
    };
    const check = () => {
      if (pc.iceGatheringState === 'complete') done();
    };
    pc.addEventListener('icegatheringstatechange', check);
    setTimeout(done, 2000);
  });
}
