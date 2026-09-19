// One conversation with the avatar backend: a WebRTC peer connection carrying the
// microphone up and the avatar's audio and video down, and a WebSocket carrying
// status, transcript and (eventually) commands. Deliberately framework-free.
import type { AgentStatusMsg, CommandMsg, ServerMessage, TranscriptMsg } from '@contracts/protocol';

export type AgentState = AgentStatusMsg['state'];

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
  close(): void;
}

export interface ConnectOptions {
  avatar: string;
  language: string;
  video: HTMLVideoElement;
  onStatus(state: AgentState): void;
  onTranscript(msg: TranscriptMsg): void;
  onCommand(msg: CommandMsg): void;
  onError(err: Error): void;
  /** Audio autoplay was refused. Recoverable, but only by a user gesture. */
  onBlocked(): void;
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
  const parts: {
    mic?: MediaStream;
    ws?: WebSocket;
    pc?: RTCPeerConnection;
    session?: SessionInfo;
  } = {};

  const close = () => {
    parts.pc?.close();
    parts.ws?.close();
    parts.mic?.getTracks().forEach((t) => t.stop());
    const s = parts.session;
    if (!s) return;
    // Fire and forget: the backend sweeps sessions the browser fails to close.
    fetch(`/sessions/${s.session_id}`, {
      method: 'DELETE',
      headers: { 'X-Control-Token': s.control_token },
    }).catch(() => {});
  };

  try {
    // The microphone first. A refusal here is the common failure and it must not
    // leave a half-built session sitting on the backend.
    parts.mic = await navigator.mediaDevices.getUserMedia({ audio: MIC });
    parts.session = await createSession(opts.avatar, opts.language);
    parts.ws = openControl(parts.session, opts);
    parts.pc = await openMedia(parts.session, parts.mic, opts);
    return {
      sessionId: parts.session.session_id,
      avatar: parts.session.avatar,
      language: parts.session.language,
      close,
    };
  } catch (err) {
    close();
    throw err;
  }
}

async function createSession(avatar: string, language: string): Promise<SessionInfo> {
  const res = await fetch('/sessions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ avatar, language }),
  });
  if (!res.ok) throw new Error(`POST /sessions failed (${res.status})`);
  return (await res.json()) as SessionInfo;
}

function openControl(session: SessionInfo, opts: ConnectOptions): WebSocket {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(
    `${proto}://${location.host}${session.control_url}?token=${session.control_token}`,
  );
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data as string) as ServerMessage;
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
        opts.onError(new Error(`${msg.code}: ${msg.message}`));
        break;
    }
  };
  ws.onerror = () => opts.onError(new Error('control socket failed'));
  return ws;
}

async function openMedia(
  session: SessionInfo,
  mic: MediaStream,
  opts: ConnectOptions,
): Promise<RTCPeerConnection> {
  const pc = new RTCPeerConnection();
  mic.getTracks().forEach((t) => pc.addTrack(t, mic));
  pc.addTransceiver('video', { direction: 'recvonly' });

  const remote = new MediaStream();
  opts.video.srcObject = remote;
  pc.ontrack = (ev) => {
    remote.addTrack(ev.track);
    opts.video.play().catch(() => opts.onBlocked());
  };

  await pc.setLocalDescription(await pc.createOffer());
  await iceGathered(pc);

  const url = `${session.offer_url}?token=${session.control_token}&avatar=true&halfduplex=false`;
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sdp: pc.localDescription?.sdp, type: pc.localDescription?.type }),
  });
  if (!res.ok) throw new Error(`WebRTC offer rejected (${res.status})`);
  const answer = (await res.json()) as { sdp: string; type: RTCSdpType };
  await pc.setRemoteDescription(answer);
  return pc;
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
