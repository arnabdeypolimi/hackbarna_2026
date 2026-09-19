// tv-avatar engineering console.
// Two planes, as in the backend: WebRTC for media (mic up, avatar A/V down)
// and a WebSocket for control (agent_status / transcript / command events).

const V = 1;
const $ = (id) => document.getElementById(id);

const el = {
  video: $("video"), portrait: $("portrait"), fps: $("fps"), res: $("res"),
  status: $("status"), statusLabel: $("status-label"),
  mic: $("mic-level"), connect: $("connect"), avatar: $("avatar"), halfduplex: $("halfduplex"),
  transcript: $("transcript"), turns: $("turns"), wire: $("wire"),
  idSession: $("id-session"), idPc: $("id-pc"), idIce: $("id-ice"), idWs: $("id-ws"),
  rtt: $("rtc-rtt"), jitter: $("rtc-jitter"), lost: $("rtc-lost"), vbr: $("rtc-vbr"), abr: $("rtc-abr"),
};

const state = {
  session: null, pc: null, ws: null, mic: null, audioCtx: null,
  timers: [], turn: null, turnCount: 0, partialRow: null,
  prevStats: null, t0: performance.now(),
};

// ---- wire log ---------------------------------------------------------

const stamp = () => ((performance.now() - state.t0) / 1000).toFixed(2).padStart(7);

function wire(kind, text) {
  const line = document.createElement("span");
  line.className = kind;
  line.innerHTML = `<span class="t">${stamp()}</span> ${kind === "in" ? "<" : kind === "out" ? ">" : "·"} ${escape(text)}\n`;
  el.wire.appendChild(line);
  el.wire.scrollTop = el.wire.scrollHeight;
}
const escape = (s) => s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
$("clear-wire").onclick = () => (el.wire.textContent = "");

// ---- status + turn timing --------------------------------------------

function setStatus(s) {
  el.status.dataset.state = s;
  el.statusLabel.textContent = s;
  const now = performance.now();
  switch (s) {
    case "thinking":
      state.turn = { n: ++state.turnCount, stop: now, first: null, done: null };
      break;
    case "speaking":
      if (state.turn && state.turn.first === null) { state.turn.first = now; renderTurn(state.turn); }
      break;
    case "idle":
      if (state.turn && state.turn.first !== null && state.turn.done === null) {
        state.turn.done = now; renderTurn(state.turn);
      }
      break;
  }
}

function renderTurn(t) {
  const think = t.first - t.stop;
  const speak = t.done ? t.done - t.first : 0;
  const total = think + speak || 1;
  let li = el.turns.querySelector(`[data-n="${t.n}"]`);
  if (!li) {
    li = document.createElement("li");
    li.dataset.n = t.n;
    el.turns.prepend(li);
    while (el.turns.children.length > 6) el.turns.lastChild.remove();
  }
  const grade = think < 1200 ? "good" : think < 2000 ? "" : "slow";
  li.innerHTML = `
    <span class="n num">#${t.n}</span>
    <span class="bar">
      <span class="seg think" style="width:${(think / total) * 100}%" title="user stop → first word"></span>
      <span class="seg speak" style="width:${(speak / total) * 100}%" title="speaking"></span>
    </span>
    <span class="lat num" data-grade="${grade}"><b>${Math.round(think)}</b> ms${t.done ? ` · ${(speak / 1000).toFixed(1)}s` : ""}</span>`;
}

// ---- transcript -------------------------------------------------------

function addTranscript({ role, text, final }) {
  if (!text.trim()) return;
  el.transcript.querySelector(".empty")?.remove();
  if (role === "user" && state.partialRow) {
    state.partialRow.dataset.final = final;
    state.partialRow.querySelector(".text").textContent = text;
    if (final) state.partialRow = null;
  } else if (role === "assistant" && lastRow()?.dataset.role === "assistant" && lastRow().dataset.open === "true") {
    lastRow().querySelector(".text").textContent += " " + text;
  } else {
    const li = document.createElement("li");
    li.dataset.role = role;
    li.dataset.final = final;
    li.dataset.open = role === "assistant" ? "true" : "false";
    li.innerHTML = `<span class="role">${role === "user" ? "you" : "avatar"}</span><span class="text"></span><span class="at num">${stamp().trim()}</span>`;
    li.querySelector(".text").textContent = text;
    el.transcript.appendChild(li);
    if (role === "user" && !final) state.partialRow = li;
  }
  el.transcript.scrollTop = el.transcript.scrollHeight;
}
const lastRow = () => el.transcript.lastElementChild;
// A user turn closes the avatar's running paragraph.
const closeAssistantRow = () => { const r = lastRow(); if (r?.dataset.role === "assistant") r.dataset.open = "false"; };

// ---- control websocket ------------------------------------------------

function openControl(session) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}${session.control_url}?token=${session.control_token}`);
  ws.onopen = () => { tone(el.idWs, "open", "ok"); wire("sys", "control socket open"); };
  ws.onclose = () => { tone(el.idWs, "closed", "bad"); wire("sys", "control socket closed"); };
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    switch (msg.type) {
      case "agent_status":
        if (msg.state === "listening") closeAssistantRow();
        setStatus(msg.state);
        wire("in", `agent_status ${msg.state}`);
        break;
      case "transcript":
        addTranscript(msg);
        if (msg.final) wire("in", `${msg.role}: ${msg.text}`);
        break;
      case "command":
        wire("in", `command ${msg.verb} ${JSON.stringify(msg.args)} turn=${msg.turn_id}`);
        break;
      case "error":
        wire("err", `${msg.code}: ${msg.message}`);
        break;
      default:
        wire("in", ev.data);
    }
  };
  return ws;
}

// ---- webrtc -----------------------------------------------------------

async function openMedia(session, withAvatar, halfDuplex) {
  const mic = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  });
  state.mic = mic;
  meter(mic);

  const pc = new RTCPeerConnection();
  state.pc = pc;
  mic.getTracks().forEach((t) => pc.addTrack(t, mic));
  if (withAvatar) pc.addTransceiver("video", { direction: "recvonly" });

  const remote = new MediaStream();
  el.video.srcObject = remote;
  pc.ontrack = (ev) => {
    remote.addTrack(ev.track);
    wire("sys", `remote ${ev.track.kind} track`);
    if (ev.track.kind === "video") { el.portrait.dataset.state = "live"; countFrames(); }
    el.video.play().catch(() => {});
  };
  pc.oniceconnectionstatechange = () => {
    const s = pc.iceConnectionState;
    tone(el.idIce, s, s === "connected" || s === "completed" ? "ok" : s === "failed" || s === "disconnected" ? "bad" : "busy");
    if (s === "failed" || s === "closed") hangup();
  };

  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  await iceGathered(pc);

  const url = `${session.offer_url}?token=${session.control_token}&avatar=${withAvatar}&halfduplex=${halfDuplex}`;
  wire("out", `POST ${url}`);
  const res = await fetch(url, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ sdp: pc.localDescription.sdp, type: pc.localDescription.type }),
  });
  if (!res.ok) throw new Error(`offer failed ${res.status}: ${await res.text()}`);
  const answer = await res.json();
  el.idPc.textContent = answer.pc_id ?? "—";
  await pc.setRemoteDescription({ sdp: answer.sdp, type: answer.type });
  wire("in", `answer pc_id=${answer.pc_id}`);
}

// Vanilla ICE: local dev needs no trickle, and one round trip is simpler to read on the wire.
const iceGathered = (pc) => new Promise((resolve) => {
  if (pc.iceGatheringState === "complete") return resolve();
  const done = () => { pc.removeEventListener("icegatheringstatechange", check); resolve(); };
  const check = () => pc.iceGatheringState === "complete" && done();
  pc.addEventListener("icegatheringstatechange", check);
  setTimeout(done, 2000);
});

function countFrames() {
  if (!("requestVideoFrameCallback" in HTMLVideoElement.prototype)) return;
  let frames = 0, last = performance.now();
  const tick = (_now, meta) => {
    frames++;
    const now = performance.now();
    if (now - last >= 1000) {
      el.fps.textContent = `${(frames * 1000 / (now - last)).toFixed(1)} fps`;
      el.res.textContent = `${meta.width}×${meta.height}`;
      frames = 0; last = now;
    }
    if (state.pc) el.video.requestVideoFrameCallback(tick);
  };
  el.video.requestVideoFrameCallback(tick);
}

async function pollStats() {
  if (!state.pc) return;
  const report = await state.pc.getStats();
  let video = null, audio = null, pair = null;
  report.forEach((s) => {
    if (s.type === "inbound-rtp" && s.kind === "video") video = s;
    if (s.type === "inbound-rtp" && s.kind === "audio") audio = s;
    if (s.type === "candidate-pair" && s.nominated && s.state === "succeeded") pair = s;
  });
  const prev = state.prevStats ?? {};
  const kbps = (cur, old) => (cur && old ? (((cur.bytesReceived - old.bytesReceived) * 8) / 1000).toFixed(0) : "—");
  el.rtt.textContent = pair?.currentRoundTripTime != null ? `${Math.round(pair.currentRoundTripTime * 1000)} ms` : "—";
  el.jitter.textContent = audio?.jitter != null ? `${Math.round(audio.jitter * 1000)} ms` : "—";
  el.lost.textContent = `${(video?.packetsLost ?? 0) + (audio?.packetsLost ?? 0)}`;
  el.vbr.textContent = kbps(video, prev.video);
  el.abr.textContent = kbps(audio, prev.audio);
  if (video?.framesPerSecond && !("requestVideoFrameCallback" in HTMLVideoElement.prototype)) {
    el.fps.textContent = `${video.framesPerSecond} fps`;
    el.res.textContent = `${video.frameWidth}×${video.frameHeight}`;
  }
  state.prevStats = { video, audio };
}

function meter(stream) {
  const ctx = new AudioContext();
  state.audioCtx = ctx;
  const src = ctx.createMediaStreamSource(stream);
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 512;
  src.connect(analyser);
  const buf = new Uint8Array(analyser.fftSize);
  const tick = () => {
    if (!state.audioCtx) return;
    analyser.getByteTimeDomainData(buf);
    let sum = 0;
    for (const v of buf) { const d = (v - 128) / 128; sum += d * d; }
    const rms = Math.sqrt(sum / buf.length);
    el.mic.style.transform = `scaleX(${Math.min(1, rms * 4)})`;
    requestAnimationFrame(tick);
  };
  tick();
}

// ---- lifecycle --------------------------------------------------------

async function connect() {
  el.connect.disabled = true;
  el.connect.textContent = "Connecting…";
  try {
    let userId = localStorage.getItem("tv_user_id");
    if (!userId) { userId = "couch_" + Math.random().toString(36).slice(2, 8); localStorage.setItem("tv_user_id", userId); }
    const session = await (await fetch("/sessions", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ user_id: userId }),
    })).json();
    state.session = session;
    el.idSession.textContent = session.session_id;
    wire("sys", `session ${session.session_id} · protocol v${session.protocol_version}`);
    state.ws = openControl(session);
    await openMedia(session, el.avatar.checked, el.halfduplex.checked);
    state.timers.push(setInterval(pollStats, 1000));
    el.connect.textContent = "Hang up";
    el.connect.dataset.live = "true";
    el.avatar.disabled = el.halfduplex.disabled = true;
    setStatus("idle");
  } catch (err) {
    wire("err", err.message);
    await hangup();
  } finally {
    el.connect.disabled = false;
  }
}

async function hangup() {
  state.timers.forEach(clearInterval);
  state.timers = [];
  state.pc?.close();
  state.ws?.close();
  state.mic?.getTracks().forEach((t) => t.stop());
  await state.audioCtx?.close().catch(() => {});
  const s = state.session;
  if (s) fetch(`/sessions/${s.session_id}`, { method: "DELETE", headers: { "X-Control-Token": s.control_token } }).catch(() => {});
  Object.assign(state, { session: null, pc: null, ws: null, mic: null, audioCtx: null, prevStats: null, turn: null, partialRow: null });
  el.portrait.dataset.state = "off";
  el.video.srcObject = null;
  el.mic.style.transform = "scaleX(0)";
  el.fps.textContent = "— fps"; el.res.textContent = "—";
  el.idPc.textContent = "—"; tone(el.idIce, "new", ""); tone(el.idWs, "closed", "");
  el.connect.textContent = "Connect";
  delete el.connect.dataset.live;
  el.avatar.disabled = el.halfduplex.disabled = false;
  el.status.dataset.state = "off";
  el.statusLabel.textContent = "disconnected";
}

function tone(node, text, t) { node.textContent = text; node.dataset.tone = t; }

el.connect.onclick = () => (state.session ? hangup() : connect());
window.addEventListener("beforeunload", () => { if (state.session) hangup(); });

fetch("/config").then((r) => r.json()).then((c) => {
  $("stack-llm").textContent = `${c.llm_model} · agent=${c.agent_impl}${c.catalog_titles ? ` · ${c.catalog_titles} titles` : ""}`;
  $("stack-stt").textContent = c.stt_model;
  $("stack-tts").textContent = `${c.tts_model} · ${c.tts_sample_rate / 1000} kHz`;
  if (!c.configured) wire("err", `missing in .env: ${c.missing.join(", ")}`);
}).catch(() => {});
