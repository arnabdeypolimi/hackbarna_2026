# Frontend Avatar Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put the talking avatar into the TV app's right-hand panel, with a language picker that chooses both the spoken language and the avatar who speaks it.

**Architecture:** The React TV app (`frontend/`) gains a framework-free session client that speaks the backend's two planes — WebRTC for media, a WebSocket for control — plus a React hook that owns the connection's phase machine and a panel component that renders it. A Vite dev proxy puts the frontend and the FastAPI backend on one origin, so no backend file changes on this branch.

**Tech Stack:** React 18, TypeScript 5.8 (strict), Vite 7, WebRTC, WebSocket. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-19-frontend-avatar-panel-design.md`

## Global Constraints

- **Work only inside `frontend/`** (plus this plan's own docs). No file under `src/tv_avatar/`, `tests/`, `contracts/` or `tools/` is touched. Never hand-edit anything in `contracts/` — it is generated.
- **Never run `uv` inside `frontend/`, never run `npm` outside it.** Two toolchains, two applications.
- **Chrome 84 is the floor** (Titan OS TVs, 2020–2022). Banned because they postdate it: `replaceChildren`, `Array.prototype.at`, `structuredClone`, `String.replaceAll`, CSS `inset`, `aspect-ratio`, `:has`, `backdrop-filter`. `tools/demo/demo.js` uses `replaceChildren` — do not copy that line across. Optional chaining, `??`, `Array.includes` and `-webkit-line-clamp` are all fine.
- **Performance budget:** nothing per-frame and nothing per-second. No `getStats()` polling, no `AudioContext` meter, no `requestVideoFrameCallback`. The target board has 1–1.5 GB of shared memory and a weak GPU.
- **Style:** 2-space indent, single quotes, no semicolon-free lines (the codebase uses semicolons). Comments explain *why*, matching the existing density — this codebase comments decisions, not mechanics.
- **Commits:** Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`). This is a **GitHub** repository; `.claude/rules.md` says GitLab and is wrong on that point.
- **Branch:** `feat/frontend-avatar-panel`, already created and already carrying the spec commit.
- **The only build gate is `npm run build`** (which is `tsc --noEmit && vite build`). `frontend/` has no test runner and this branch does not add one — that was a deliberate decision recorded in the spec. Every task therefore ends with a build check *and* a named manual observation.
- **A running backend is needed from Task 2 onward:** `uv run uvicorn tv_avatar.app:app --reload --port 8000` from the repository root, in its own terminal, with a `.env` carrying the provider keys.

---

## File Structure

**Created**

| File | Responsibility |
| --- | --- |
| `frontend/src/lib/avatarClient.ts` | One session: create, connect both planes, tear down. No React. |
| `frontend/src/lib/avatarCatalog.ts` | Pure derivations over `/config`: which languages to offer, which avatar speaks one. |
| `frontend/src/hooks/useAvatar.ts` | The phase machine and all React state for the panel. |
| `frontend/src/components/AvatarPanel.tsx` | Renders a phase into the right-hand panel. |
| `frontend/src/components/LanguagePicker.tsx` | Remote-navigable language chips. |

**Modified**

| File | Change |
| --- | --- |
| `frontend/vite.config.ts` | Dev proxy for `/config` and `/sessions` (HTTP + WebSocket). |
| `frontend/tsconfig.json` | `@contracts/*` path mapping so wire types come from the generated file. |
| `frontend/src/App.tsx` | Swap `ResumePanel` for `AvatarPanel`; drop the resume derivation. |
| `frontend/src/lib/rows.ts` | Delete `pickResume()`. |
| `frontend/src/styles.css` | Replace the resume panel's rules with the avatar panel's. |
| `frontend/README.md` | Document the backend dependency and the proxy. |

**Deleted**

| File | Why |
| --- | --- |
| `frontend/src/components/ResumePanel.tsx` | The avatar takes its slot outright. |

---

### Task 1: Dev topology — proxy and contract types

Puts the frontend and backend on one origin and lets the app import the generated wire types. Nothing visible changes; the deliverable is that a request to the Vite dev server reaches FastAPI and that a type-only import from `contracts/` compiles.

**Files:**
- Modify: `frontend/vite.config.ts`
- Modify: `frontend/tsconfig.json`

**Interfaces:**
- Consumes: nothing.
- Produces: the module specifier `@contracts/protocol`, resolving to `contracts/protocol.d.ts`. Every later task imports its wire types from there.

- [ ] **Step 1: Add the dev proxy**

Replace the whole of `frontend/vite.config.ts` with:

```ts
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
```

- [ ] **Step 2: Point TypeScript at the generated contracts**

In `frontend/tsconfig.json`, add `paths` to `compilerOptions` and extend `include`. The file becomes:

```json
{
  "compilerOptions": {
    "target": "ES2020",
    "lib": ["ES2020", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "moduleResolution": "bundler",
    "jsx": "react-jsx",
    "strict": true,
    "noEmit": true,
    "skipLibCheck": true,
    "isolatedModules": true,
    "verbatimModuleSyntax": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "noFallthroughCasesInSwitch": true,
    "paths": { "@contracts/*": ["../contracts/*"] }
  },
  "include": ["src", "../contracts"]
}
```

No Vite alias is needed to match this. Wire types are imported **type-only**, so the import is erased before the bundler ever sees it.

- [ ] **Step 3: Verify the types resolve**

Create a scratch file `frontend/src/lib/__probe.ts`:

```ts
import type { ServerMessage } from '@contracts/protocol';

export const probe: ServerMessage = { v: 1, type: 'agent_status', state: 'idle' };
```

Run from `frontend/`: `npx tsc --noEmit`
Expected: PASS, no errors.

Then change `state: 'idle'` to `state: 'nonsense'` and run it again.
Expected: FAIL with a message naming the four allowed states. This proves the mapping resolves to the real generated file and not to `any`.

Delete the probe file: `rm src/lib/__probe.ts`

- [ ] **Step 4: Verify the proxy carries a real backend response**

With `uv run uvicorn tv_avatar.app:app --port 8000` running at the repository root, start `npm run dev` in `frontend/` and run:

```bash
curl -s http://localhost:5173/config
```

Expected: the backend's JSON, containing `"default_avatar"`, an `avatars` array of five entries and a `languages` array of four. If it returns HTML, the proxy is not matching; if it returns a connection error, uvicorn is not running.

- [ ] **Step 5: Verify the build still passes**

Run from `frontend/`: `npm run build`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/vite.config.ts frontend/tsconfig.json
git commit -m "feat(frontend): proxy the avatar backend and import the generated protocol types"
```

---

### Task 2: The session client

The whole conversation with the backend, with no React in it. Deliverable: from the browser console, one call opens a session, shows a face and plays audio, and a second call tears it down.

**Files:**
- Create: `frontend/src/lib/avatarClient.ts`
- Create: `frontend/src/lib/avatarCatalog.ts`

**Interfaces:**
- Consumes: `@contracts/protocol` (Task 1).
- Produces:
  - `fetchConfig(): Promise<BackendConfig>`
  - `connect(opts: ConnectOptions): Promise<AvatarSession>` where `AvatarSession = { sessionId: string; avatar: string; language: string; close(): void }`
  - `ConnectOptions = { avatar: string; language: string; video: HTMLVideoElement; onStatus(s: AgentState): void; onTranscript(m: TranscriptMsg): void; onCommand(m: CommandMsg): void; onError(e: Error): void; onBlocked(): void }`
  - `AgentState = 'idle' | 'listening' | 'thinking' | 'speaking'`
  - types `BackendConfig`, `AvatarInfo`, `LanguageInfo`
  - `languageOptions(config: BackendConfig): LanguageInfo[]`
  - `avatarForLanguage(config: BackendConfig, code: string): AvatarInfo | null`

- [ ] **Step 1: Write the session client**

Create `frontend/src/lib/avatarClient.ts`:

```ts
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
  // Owned here until it is handed back. A throw between construction and the return
  // would otherwise strand it: the caller's cleanup reads a variable this function
  // never got to assign, and a peer connection is not reclaimed by losing its last
  // reference — its ICE agent and DTLS state stay alive. The backend answering a
  // misconfigured offer with 503 is the everyday way to hit that.
  try {
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
```

- [ ] **Step 2: Write the catalog helpers**

Create `frontend/src/lib/avatarCatalog.ts`:

```ts
// Pure derivations over /config. The language-to-avatar mapping lives here and
// nowhere else, and it is never hardcoded: avatars.yaml is free to change under us.
import type { AvatarInfo, BackendConfig, LanguageInfo } from './avatarClient';

/** The languages at least one avatar can actually speak, in catalog order. */
export function languageOptions(config: BackendConfig): LanguageInfo[] {
  return config.languages.filter((l) => config.avatars.some((a) => a.languages.indexOf(l.code) >= 0));
}

/**
 * Who speaks a language. A specialist beats the multilingual default — Lucía's
 * Castilian voice is the entire point of having her — so the narrowest allow-list
 * covering the language wins. The catalogue's default avatar breaks ties, and takes
 * the default language outright: that is the front door, and the default is the host
 * the catalogue means a viewer to meet there.
 *
 * The rule cannot be "prefer the default", which is the obvious reading and is wrong:
 * an avatar with no `languages` key in avatars.yaml is published as speaking every
 * language, so the default would win all four and the native voices would be dead
 * entries.
 */
export function avatarForLanguage(config: BackendConfig, code: string): AvatarInfo | null {
  const able = config.avatars.filter((a) => a.languages.indexOf(code) >= 0);
  if (!able.length) return null;
  const fallback = able.filter((a) => a.id === config.default_avatar)[0];
  if (code === config.default_language && fallback) return fallback;
  const narrowest = able.reduce((a, b) => (b.languages.length < a.languages.length ? b : a));
  const tied = able.filter((a) => a.languages.length === narrowest.languages.length);
  return tied.filter((a) => a.id === config.default_avatar)[0] ?? tied[0];
}
```

- [ ] **Step 3: Verify it compiles**

Run from `frontend/`: `npm run build`
Expected: PASS. If `tsc` reports the modules are unused, that is expected — `noUnusedLocals` does not flag unused exports, so a failure here is a real error.

- [ ] **Step 4: Smoke-test against the live backend**

With uvicorn on `:8000` and `npm run dev` running, open `http://localhost:5173`, open the browser console and run:

```js
const c = await import('/src/lib/avatarClient.ts');
const k = await import('/src/lib/avatarCatalog.ts');
const cfg = await c.fetchConfig();
console.log(k.languageOptions(cfg).map(l => l.code), k.avatarForLanguage(cfg, 'es')?.name);

const v = document.createElement('video');
v.autoplay = true; v.playsInline = true;
v.style.cssText = 'position:fixed;right:0;bottom:0;width:320px;z-index:9999';
document.body.appendChild(v);

const s = await c.connect({
  avatar: 'cara', language: 'en', video: v,
  onStatus: (x) => console.log('status', x),
  onTranscript: (m) => console.log('text', m.role, m.text),
  onCommand: (m) => console.log('command', m.verb),
  onError: (e) => console.error(e),
  onBlocked: () => console.warn('autoplay blocked'),
});
```

Expected, in order: the language codes `['en','es','fr','ca']`, and the avatar for each of `en`/`es`/`fr`/`ca` being `Cara`/`Lucía`/`Chloé`/`Pau` — all four, because the whole mapping is what is under test; a microphone permission prompt; a face appearing in the corner video within a few seconds; the avatar greeting you audibly; `status` logging `listening` / `thinking` / `speaking` as you speak to it.

Then run `s.close(); v.remove();`
Expected: the video stops, the browser's microphone indicator goes out, and the uvicorn log shows the session closing.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/avatarClient.ts frontend/src/lib/avatarCatalog.ts
git commit -m "feat(frontend): add the avatar session client and catalogue helpers"
```

---

### Task 3: The panel in the layout

The right-hand panel becomes the avatar's, and the resume panel leaves. No connection yet — the panel is driven by a hard-coded view object that Task 4 deletes. Deliverable: the TV app renders the new panel in the old slot, and every phase can be eyeballed by editing one literal.

**Files:**
- Create: `frontend/src/components/AvatarPanel.tsx`
- Create: `frontend/src/components/LanguagePicker.tsx`
- Modify: `frontend/src/styles.css` (replace the `.side-*` / `.resume-*` rules, lines 145–199)
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/lib/rows.ts`
- Delete: `frontend/src/components/ResumePanel.tsx`

**Interfaces:**
- Consumes: `LanguageInfo`, `AvatarInfo`, `AgentState` from `lib/avatarClient` (Task 2).
- Produces:
  - `Phase = 'off' | 'connecting' | 'live' | 'blocked' | 'error'`
  - `AvatarView = { phase: Phase; message: string; avatar: AvatarInfo | null; status: AgentState; lastLine: string; languages: LanguageInfo[]; language: string; setLanguage(code: string): void; retry(): void }` — exported from `components/AvatarPanel`, and the exact shape Task 4's hook must return.
  - `<AvatarPanel view={AvatarView} videoRef={RefObject<HTMLVideoElement>} />`
  - `<LanguagePicker options={LanguageInfo[]} value={string} onPick={(code: string) => void} />`

- [ ] **Step 1: Write the language picker**

Create `frontend/src/components/LanguagePicker.tsx`:

```tsx
import type { LanguageInfo } from '../lib/avatarClient';

interface Props {
  options: LanguageInfo[];
  value: string;
  onPick: (code: string) => void;
}

/**
 * Chips, not a <select>: a native dropdown on a television opens a list the remote
 * cannot steer well. Each chip carries `.f`, which is all spatial navigation needs
 * to find it.
 *
 * Never disabled, not even mid-connection. The app connects on load, so a picker
 * that greys out while connecting is dead for the first seconds of every session
 * and forever if the backend hangs — and a disabled button is skipped by spatial
 * navigation, which would strand the whole panel. A press during a connection
 * supersedes it; `useAvatar`'s generation counter exists for exactly that.
 */
export function LanguagePicker({ options, value, onPick }: Props) {
  if (!options.length) return null;
  return (
    <div className="langs" role="group" aria-label="Avatar language">
      {options.map((l) => (
        <button
          key={l.code}
          className={l.code === value ? 'lang cur f' : 'lang f'}
          aria-label={`Speak ${l.name}`}
          aria-pressed={l.code === value}
          onClick={() => onPick(l.code)}
        >
          {l.native_name}
        </button>
      ))}
    </div>
  );
}
```

- [ ] **Step 2: Write the panel**

Create `frontend/src/components/AvatarPanel.tsx`:

```tsx
import type { RefObject } from 'react';
import type { AgentState, AvatarInfo, LanguageInfo } from '../lib/avatarClient';
import { LanguagePicker } from './LanguagePicker';

export type Phase = 'off' | 'connecting' | 'live' | 'blocked' | 'error';

/** Everything the panel renders. Task 4's useAvatar hook returns exactly this. */
export interface AvatarView {
  phase: Phase;
  /** What the panel says while it is not live: a reason, never a blank rectangle. */
  message: string;
  avatar: AvatarInfo | null;
  status: AgentState;
  lastLine: string;
  languages: LanguageInfo[];
  language: string;
  setLanguage: (code: string) => void;
  retry: () => void;
}

interface Props {
  view: AvatarView;
  videoRef: RefObject<HTMLVideoElement>;
}

const STATUS_LABEL: Record<AgentState, string> = {
  idle: 'ready',
  listening: 'listening',
  thinking: 'thinking',
  speaking: 'speaking',
};

export function AvatarPanel({ view, videoRef }: Props) {
  const live = view.phase === 'live';
  const name = view.avatar?.name ?? 'the avatar';
  return (
    <aside className="panel side avatarpanel">
      <div className="face" data-state={live ? 'live' : 'off'}>
        <video ref={videoRef} autoPlay playsInline />
        {!live && <p className="face-note">{view.message}</p>}
      </div>

      {live ? (
        <>
          <p className="avatar-name">
            {name}
            <span className="pill" data-state={view.status}>{STATUS_LABEL[view.status]}</span>
          </p>
          <p className="say" aria-live="polite">{view.lastLine || 'Say hello.'}</p>
        </>
      ) : (
        <div className="side-actions">
          <button
            className="btn wide primary f"
            disabled={view.phase === 'connecting'}
            onClick={view.retry}
          >
            {view.phase === 'connecting' ? 'Connecting…' : `Talk to ${name}`}
          </button>
        </div>
      )}

      <LanguagePicker
        options={view.languages}
        value={view.language}
        onPick={view.setLanguage}
      />
    </aside>
  );
}
```

- [ ] **Step 3: Replace the resume panel's styles**

In `frontend/src/styles.css`, delete these rules. Find them by selector rather than by line number — each deletion shifts the ones below it. Every one exists only for the panel being removed:

`.side-empty`, `.resume-art`, `.side h3`, `.side .chips`, `.side .label`, `.side .desc`

**Keep** `.side` itself (the slot, the tilt) and `.side-actions` (the retry button uses it). Afterwards, `grep -n 'resume\|side-empty' src/styles.css` must return nothing.

Then append, after the `.side-actions` rule:

```css
/* The avatar panel keeps the resume panel's slot, glass and tilt. The room has one
   right-hand plane and the face belongs on it, not floating in front of it. */
.avatarpanel { gap: var(--gap-group); }

/* Anam sends a 2:3 portrait. At the panel's 468px of inner width that is 702px tall,
   which would leave 126px for everything else, so the frame is cropped to head and
   shoulders — the part of a talking head that carries at ten feet anyway. */
.face {
  position: relative; flex: none; height: 600px;
  border-radius: 30px; overflow: hidden; background: rgba(0, 0, 0, 0.35);
}
.face video {
  display: block; width: 100%; height: 100%;
  object-fit: cover; object-position: top;
  opacity: 0; transition: opacity 400ms ease-out;
}
.face[data-state="live"] video { opacity: 1; }
.face-note {
  position: absolute; left: var(--gap-block); right: var(--gap-block); top: 50%;
  margin: 0; transform: translateY(-50%);
  text-align: center; font-size: 22px; line-height: 1.4; color: var(--ink-2);
}

.avatar-name {
  margin: 0; font-size: 34px; font-weight: 600;
  display: flex; align-items: center; gap: var(--gap-group);
}
/* The pill is the only moving part of the panel, so the four agent states have to be
   told apart at a glance and without reading: value for speaking, hue for listening. */
.pill {
  padding: 4px 14px; border-radius: 16px; background: var(--chip-quiet);
  font-size: 16px; font-weight: 500; color: var(--ink-2);
}
.pill[data-state="listening"] { background: rgba(95, 208, 187, 0.22); color: var(--kids); }
.pill[data-state="speaking"] { background: rgba(255, 255, 255, 0.28); color: var(--ink); }

.say {
  margin: 0; font-size: 21px; line-height: 1.4; color: var(--ink-2);
  display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2;
  overflow: hidden; height: 60px;
}

/* margin-top: auto pins the picker to the bottom of the panel whatever is above it. */
.langs { display: flex; flex-wrap: wrap; gap: var(--gap-item); margin-top: auto; }
.lang {
  padding: 10px 22px; border-radius: 26px;
  background: var(--chip); font-size: 19px; color: var(--ink-2);
}
.lang.cur { background: var(--primary); color: var(--ink); box-shadow: inset 0 0 0 1.5px var(--primary-edge); }
.lang:focus { outline: none; background: var(--focus-bg); color: var(--focus-ink); box-shadow: none; }
```

- [ ] **Step 4: Delete the resume panel and its data**

```bash
rm frontend/src/components/ResumePanel.tsx
```

In `frontend/src/lib/rows.ts`, delete the whole `pickResume` function and its doc comment (the last block in the file).

- [ ] **Step 5: Wire the panel into App with a temporary view**

In `frontend/src/App.tsx`:

Change the rows import — drop `pickResume`:

```ts
import { buildRow, TAB_TITLES } from './lib/rows';
```

Replace the `ResumePanel` import with:

```ts
import { AvatarPanel, type AvatarView } from './components/AvatarPanel';
```

The watch-history state is now written but never read, so name it accordingly or `noUnusedLocals` fails the build. Replace:

```ts
  const [history, setHistory] = useState<Record<string, number>>(() => readJSON(historyKey(activeId), {}));
```

with:

```ts
  // Write-only since the resume panel left: nothing renders history today, but Watch
  // keeps recording it because the agent will want it.
  const [, setHistory] = useState<Record<string, number>>(() => readJSON(historyKey(activeId), {}));
```

Delete the resume derivation:

```ts
  const resume = useMemo(() => pickResume(catalog, history), [catalog, history]);
```

Add a video ref beside the other refs:

```ts
  const avatarVideo = useRef<HTMLVideoElement>(null);
```

Add the temporary view immediately above the `// ---------- render ----------` comment. **Task 4 deletes this block** — it exists so the panel can be seen before the connection works:

```ts
  // TEMPORARY (Task 3 only): a hand-held view so the panel can be laid out before
  // useAvatar exists. Edit `phase` to eyeball each state. Task 4 replaces it.
  const avatar: AvatarView = {
    phase: 'connecting',
    message: 'Connecting to Cara…',
    avatar: { id: 'cara', name: 'Cara', description: '', avatar_model: 'cara-4', languages: ['en', 'es', 'fr', 'ca'] },
    status: 'listening',
    lastLine: 'Show me something with dragons in it.',
    languages: [
      { code: 'en', name: 'English', native_name: 'English' },
      { code: 'es', name: 'Spanish', native_name: 'Español' },
      { code: 'fr', name: 'French', native_name: 'Français' },
      { code: 'ca', name: 'Catalan', native_name: 'Català' },
    ],
    language: 'en',
    setLanguage: () => {},
    retry: () => {},
  };
```

Replace the whole `<ResumePanel … />` element with:

```tsx
      <AvatarPanel view={avatar} videoRef={avatarVideo} />
```

- [ ] **Step 6: Verify the build**

Run from `frontend/`: `npm run build`
Expected: PASS. A `noUnusedLocals` error naming `history`, `useMemo` or `ResumePanel` means a step above was missed.

- [ ] **Step 7: Verify it on screen**

With `npm run dev`, open `http://localhost:5173`.

Expected: the right-hand panel occupies exactly the slot the resume panel did, same tilt and same glass; it shows a dark rounded frame reading "Connecting to Cara…", a disabled "Connecting…" button, and four language chips along the bottom with **English** highlighted. Arrow-key navigation from the browse panel reaches the chips, and each takes a white focus ring.

Then edit the temporary block: set `phase: 'live'`.
Expected: the frame's message disappears, and "Cara" appears with a teal `listening` pill and the dragons line beneath it.

Set it back to `phase: 'connecting'` before committing.

- [ ] **Step 8: Commit**

```bash
git add -A frontend/src
git commit -m "feat(frontend): give the right-hand panel to the avatar"
```

---

### Task 4: Connect on load

The panel starts talking. Deliverable: opening the app connects, shows the face, plays audio and tracks the agent's state, and every failure says what went wrong.

**Files:**
- Create: `frontend/src/hooks/useAvatar.ts`
- Modify: `frontend/src/App.tsx` (delete the temporary view from Task 3)

**Interfaces:**
- Consumes: `connect`, `fetchConfig`, `BackendConfig`, `AgentState` from `lib/avatarClient`; `avatarForLanguage`, `languageOptions` from `lib/avatarCatalog`; `AvatarView`, `Phase` from `components/AvatarPanel`; `readJSON`, `writeJSON` from `lib/storage`.
- Produces: `useAvatar(video: RefObject<HTMLVideoElement>): AvatarView`. Task 5 relies on its `setLanguage` doing a full reconnect.

- [ ] **Step 1: Write the hook**

Create `frontend/src/hooks/useAvatar.ts`:

```ts
import { useCallback, useEffect, useRef, useState, type RefObject } from 'react';
import { avatarForLanguage, languageOptions } from '../lib/avatarCatalog';
import { connect, fetchConfig, type AgentState, type AvatarSession, type BackendConfig } from '../lib/avatarClient';
import { readJSON, writeJSON } from '../lib/storage';
import type { AvatarView, Phase } from '../components/AvatarPanel';

// A property of the television, not of a profile: whoever sits down next hears the
// language the set was left speaking.
const LANG_KEY = 'tv.avatar.language';

export function useAvatar(video: RefObject<HTMLVideoElement>): AvatarView {
  const [config, setConfig] = useState<BackendConfig | null>(null);
  const [phase, setPhase] = useState<Phase>('off');
  const [message, setMessage] = useState('Starting…');
  const [status, setStatus] = useState<AgentState>('idle');
  const [lastLine, setLastLine] = useState('');
  const [language, setLanguageState] = useState(() => readJSON<string>(LANG_KEY, ''));

  const session = useRef<AvatarSession | null>(null);
  // One connection at a time. A language switch supersedes whatever the previous
  // attempt was doing, and that attempt's late callbacks must not write over it.
  const gen = useRef(0);

  const start = useCallback(async (cfg: BackendConfig, code: string) => {
    const mine = ++gen.current;
    session.current?.close();
    session.current = null;
    setStatus('idle');
    setLastLine('');

    const who = avatarForLanguage(cfg, code);
    if (!who) {
      setPhase('error');
      setMessage(`No avatar speaks ${code}`);
      return;
    }
    const el = video.current;
    if (!el) {
      setPhase('error');
      setMessage('No video element');
      return;
    }
    setPhase('connecting');
    setMessage(`Connecting to ${who.name}…`);

    const mineStill = () => gen.current === mine;
    try {
      const live = await connect({
        avatar: who.id,
        language: code,
        video: el,
        onStatus: (s) => { if (mineStill()) setStatus(s); },
        onTranscript: (m) => { if (mineStill() && m.text.trim()) setLastLine(m.text); },
        onCommand: (m) => {
          // TODO(M3): dispatch verbs into App state. The agent emits no commands until
          // tool calls are wired — see "Current state" in CLAUDE.md and the verb list
          // in contracts/protocol.d.ts.
          console.debug('[avatar] command', m.verb, m.args);
        },
        onError: (e) => { if (mineStill()) { setPhase('error'); setMessage(e.message); } },
        onBlocked: () => { if (mineStill()) { setPhase('blocked'); setMessage(`Press OK to hear ${who.name}`); } },
      });
      // A newer attempt started while this one was negotiating: drop this session
      // rather than leaving it running and unreferenced.
      if (!mineStill()) { live.close(); return; }
      session.current = live;
      setPhase('live');
    } catch (err) {
      if (!mineStill()) return;
      const e = err as Error;
      // A refused microphone is recoverable by a gesture; everything else is not.
      const blocked = e.name === 'NotAllowedError' || e.name === 'SecurityError';
      setPhase(blocked ? 'blocked' : 'error');
      setMessage(blocked ? `Press OK to talk to ${who.name}` : e.message);
    }
  }, [video]);

  useEffect(() => {
    let cancelled = false;
    fetchConfig()
      .then((cfg) => {
        if (cancelled) return;
        setConfig(cfg);
        if (!cfg.configured) {
          setPhase('error');
          setMessage(`Avatar unavailable — missing ${cfg.missing.join(', ')}`);
          return;
        }
        const codes = languageOptions(cfg).map((l) => l.code);
        const code = codes.indexOf(language) >= 0 ? language : cfg.default_language;
        setLanguageState(code);
        void start(cfg, code);
      })
      .catch(() => {
        if (cancelled) return;
        setPhase('error');
        setMessage('Backend not running');
      });
    return () => {
      cancelled = true;
      gen.current++;
      session.current?.close();
      session.current = null;
    };
    // Mount only: re-running this would open a second session.
  }, []);

  useEffect(() => {
    // A closed tab must not leave a session (and its provider minutes) running.
    const bye = () => session.current?.close();
    window.addEventListener('beforeunload', bye);
    return () => window.removeEventListener('beforeunload', bye);
  }, []);

  const setLanguage = (code: string) => {
    if (!config || code === language) return;
    setLanguageState(code);
    writeJSON(LANG_KEY, code);
    void start(config, code);
  };

  const retry = () => { if (config) void start(config, language); };

  return {
    phase,
    message,
    avatar: config ? avatarForLanguage(config, language) : null,
    status,
    lastLine,
    languages: config ? languageOptions(config) : [],
    language,
    setLanguage,
    retry,
  };
}
```

- [ ] **Step 2: Use the hook in App**

In `frontend/src/App.tsx`, delete the entire temporary `const avatar: AvatarView = { … };` block from Task 3, and change the import:

```ts
import { AvatarPanel } from './components/AvatarPanel';
```

(the `type AvatarView` import goes with the block that used it). Add the hook call immediately **after** `const avatarVideo = useRef<HTMLVideoElement>(null);` — it reads that ref, and `const` bindings are in the temporal dead zone until their declaration, so placing it earlier in the same scope is a compile error, not a style preference:

```ts
  const avatar = useAvatar(avatarVideo);
```

and add the import:

```ts
import { useAvatar } from './hooks/useAvatar';
```

- [ ] **Step 3: Verify the build**

Run from `frontend/`: `npm run build`
Expected: PASS.

- [ ] **Step 4: Verify the happy path**

With uvicorn on `:8000` (with a real `.env`) and `npm run dev`, open `http://localhost:5173` and allow the microphone.

Expected, in order: the panel reads "Connecting to Cara…"; within a few seconds the face fades in and the message disappears; Cara greets you audibly; the pill reads `speaking` while she talks and `listening` when she stops; speaking to her puts your words in the line below her name, then hers.

- [ ] **Step 5: Verify every failure state**

Each of these must produce a readable panel and must leave the rest of the TV app browsable:

| Do this | Panel must read |
| --- | --- |
| Stop uvicorn, reload | "Backend not running" |
| `mv .env .env.off`, restart uvicorn, reload (then `mv .env.off .env`) | "Avatar unavailable — missing …" naming the variables |
| Block the microphone in the site permissions, reload | "Press OK to talk to Cara", on a focusable button |
| From the blocked state, restore the permission and press OK | it connects |

- [ ] **Step 6: Verify the teardown**

With a live session, close the tab. Expected: the uvicorn log records the session closing, and the browser's microphone indicator goes out. Reload twice in a row and confirm the log shows one session closing for each one opened — a leak here means every hot reload during development is paying for an orphaned session.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/hooks/useAvatar.ts frontend/src/App.tsx
git commit -m "feat(frontend): connect the avatar panel on load"
```

---

### Task 5: Language switching, and the docs that go with it

The picker does something. Deliverable: choosing Español hangs up and brings back Lucía speaking Spanish, the choice survives a reload, and `frontend/README.md` tells the next person how to run the two halves together.

**Files:**
- Modify: `frontend/README.md`
- (`setLanguage` was written in Task 4; this task proves it and documents the result.)

**Interfaces:**
- Consumes: everything from Tasks 1–4.
- Produces: nothing new.

- [ ] **Step 1: Verify a language switch**

With a live English session, arrow down to the chips and press OK on **Español**.

Expected: the frame reads "Connecting to Lucía…" and the chips stay live (they are never disabled); the old face disappears; within a few seconds Lucía appears and greets you **in Spanish**; the `Español` chip is now the highlighted one. Speak Spanish to her and confirm the transcript line fills in.

Then try **Català**: Pau appears. His voice is Cartesia's Spanish model reading Catalan — that is the documented fallback in `avatars.yaml`, not a bug.

- [ ] **Step 2: Verify the switch does not leak a session**

Watch the uvicorn log across the switch. Expected: exactly one session closes and one opens. Then press three chips quickly in a row. Expected: no more sessions remain open than were opened, and the panel settles on the last one chosen — this is what the generation counter in `useAvatar` is for.

- [ ] **Step 3: Verify persistence**

With Español selected, reload the page. Expected: it connects to Lucía directly, with no English in between.

Then run `localStorage.clear()` in the console and reload. Expected: it falls back to the catalogue's default (English, Cara).

- [ ] **Step 4: Document it**

Add this section to `frontend/README.md`, immediately after whatever section covers running the dev server:

```markdown
## The avatar panel

The right-hand panel is a live conversational avatar, so the app needs the Python
backend in this repository running alongside it:

```bash
# repository root, its own terminal
uv run uvicorn tv_avatar.app:app --reload --port 8000

# frontend/
npm run dev
```

Vite proxies `/config` and `/sessions` (including the control WebSocket) to
`localhost:8000`, which keeps the app same-origin — see `vite.config.ts`. Without
the backend the panel reads "Backend not running" and the rest of the app browses
normally. Without provider keys in the backend's `.env` it reads "Avatar
unavailable" and names what is missing.

Two things worth knowing:

- **The app connects on load.** Every reload, including a hot reload, opens a paid
  Anam and Cartesia session. Stop the dev server when you are not using it.
- **On a real television it needs https.** `getUserMedia` is blocked on an insecure
  origin, so a plain `http://` LAN address cannot reach the microphone at all.
  Desktop development on `localhost` is exempt from that rule.

The language chips pick the spoken language *and* the avatar who speaks it — one
avatar per language, taken from the backend's `avatars.yaml`. Changing language
opens a new session, because a session pins its voice and persona at creation. The
choice is remembered in `localStorage` for the whole television, not per profile.

Wire types come from `contracts/protocol.d.ts`, which is **generated** from
`src/tv_avatar/agent/commands.py` — import them, never hand-write them. The agent
does not emit commands yet (phase 2 / M3); `useAvatar` logs them and there is a
`TODO(M3)` marking where they will be dispatched.
```

- [ ] **Step 5: Verify the build one last time**

Run from `frontend/`: `npm run build`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/README.md
git commit -m "docs(frontend): describe the avatar panel and its backend dependency"
```

---

## Done when

- The TV app's right panel is the avatar; the resume panel is gone from the layout and from the code.
- Opening the app connects and the avatar greets the viewer.
- Four language chips are reachable by remote; each brings the matching avatar speaking that language; the choice survives a reload.
- `/config` unreachable, `/config` unconfigured, and a refused microphone each produce a readable panel, and the catalogue still browses in all three.
- `npm run build` passes, and `uv run pytest` at the root is untouched at 81 passing (nothing on this branch touches Python).
