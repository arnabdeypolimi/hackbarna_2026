// Mock TV app. Mirrors contracts/protocol.d.ts — regenerate that file
// with `uv run python tools/export_schemas.py` after any command change.
const V = 1;
const FALLBACK_TITLES = [
  { title_id: "tt_1", name: "Heat" },
  { title_id: "tt_2", name: "Sicario" },
  { title_id: "tt_3", name: "Arrival" },
  { title_id: "tt_4", name: "Dune" },
];
let TITLES = FALLBACK_TITLES;

let ws = null;
let pc = null;
let session = null;
let focus = 0;
let playback = { state: "stopped", title_id: null, position_s: 0 };
let ticker = null;
let turnCaption = "";

const $ = (id) => document.getElementById(id);
const log = (m) => {
  const el = $("log");
  el.textContent += m + "\n";
  el.scrollTop = el.scrollHeight;
};

function userId() {
  let id = localStorage.getItem("tv_user_id");
  if (!id) {
    id = "couch_" + Math.random().toString(36).slice(2, 8);
    localStorage.setItem("tv_user_id", id);
  }
  return id;
}

function render() {
  $("grid").innerHTML = TITLES.map((t, i) => {
    const poster = t.poster_path
      ? `<img src="https://image.tmdb.org/t/p/w185${t.poster_path}" alt="" />` : "";
    const meta = t.year ? `<small>${t.year}${t.genres ? " · " + t.genres.slice(0, 2).join(", ") : ""}</small>` : "";
    return `<div class="tile ${i === focus ? "focused" : ""}" data-id="${t.title_id}">${poster}<div>${t.name}</div>${meta}</div>`;
  }).join("");
  const name = TITLES.find((t) => t.title_id === playback.title_id)?.name || playback.title_id;
  $("playback").textContent =
    playback.state === "stopped" ? "stopped" : `${playback.state} ${name} @ ${Math.floor(playback.position_s)}s`;
}

function pushScreenState() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({
    v: V,
    type: "screen_state",
    state: {
      view: playback.state === "stopped" ? "grid" : "player",
      rail_id: "rail_mock",
      focus_index: focus,
      tiles: TITLES.map((t, i) => ({ title_id: t.title_id, name: t.name, position: i })),
      playback,
    },
  }));
}

function ack(id, ok = true, error = null) {
  ws.send(JSON.stringify({ v: V, type: "ack", command_id: id, ok, error }));
}

function apply(msg) {
  const a = msg.args || {};
  switch (msg.verb) {
    case "navigate":
      focus = Math.min(TITLES.length - 1, Math.max(0,
        focus + (a.direction === "right" ? (a.count || 1)
              : a.direction === "left" ? -(a.count || 1) : 0)));
      break;
    case "focus":
    case "open_details":
      focus = Math.max(0, TITLES.findIndex((t) => t.title_id === a.title_id));
      break;
    case "play":
      playback = { state: "playing", title_id: a.title_id, position_s: a.resume_from || 0 };
      break;
    case "pause": playback.state = "paused"; break;
    case "resume": playback.state = "playing"; break;
    case "seek":
      playback.position_s = a.to_seconds ?? (playback.position_s + (a.delta_seconds || 0));
      break;
    case "back": case "home": case "close":
      playback = { state: "stopped", title_id: null, position_s: 0 };
      break;
    case "show_products":
      break;
    case "show_titles": {
      // The mock has no other rails: the picks replace the tiles, first one focused.
      const byId = new Map(TITLES.map((t) => [t.title_id, t]));
      const shown = a.title_ids.map((id) => byId.get(id) || { title_id: id, name: id });
      TITLES = shown;
      focus = 0;
      log(`rail "${a.label}": ${shown.map((t) => t.name).join(", ")}`);
      break;
    }
    case "search_catalog":
      ws.send(JSON.stringify({ v: V, type: "result", command_id: msg.id,
        data: { titles: TITLES.slice(0, a.limit || 10).map((t) => ({ title_id: t.title_id, name: t.name })) } }));
      break;
    default:
      log(`unhandled verb: ${msg.verb}`);
      ack(msg.id, false, "unhandled verb");
      return;
  }
  ack(msg.id);
  render();
  pushScreenState();
}

async function loadCatalog() {
  try {
    const res = await fetch("/catalog/sample?limit=8");
    const body = await res.json();
    if (body.titles && body.titles.length) {
      TITLES = body.titles;
      log(`catalog: ${TITLES.length} real titles`);
    } else {
      log("catalog: not built, using placeholder tiles");
    }
  } catch (e) {
    log("catalog: unavailable, using placeholder tiles");
  }
}

async function start() {
  await loadCatalog();
  const uid = userId();
  $("user").textContent = uid;
  session = await (await fetch("/sessions", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ user_id: uid }),
  })).json();
  log(`session ${session.session_id} user ${session.user_id} (protocol v${session.protocol_version})`);
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}${session.control_url}?token=${session.control_token}`);
  ws.onopen = () => { log("control socket open"); pushScreenState(); };
  ws.onclose = () => log("control socket closed");
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "command") { log(`< ${ev.data}`); apply(msg); }
    else if (msg.type === "agent_status") $("status").textContent = msg.state;
    else if (msg.type === "transcript") {
      // Assistant captions arrive per TTS sentence; accumulate within a turn.
      const el = $(msg.role === "user" ? "you" : "avatar");
      if (msg.role === "assistant") el.textContent = (turnCaption += " " + msg.text).trim();
      else { el.textContent = msg.text; if (msg.final) turnCaption = ""; }
    } else log(`< ${ev.data}`);
  };
  ticker = setInterval(() => {
    if (playback.state === "playing") { playback.position_s += 1; render(); if (playback.position_s % 5 === 0) pushScreenState(); }
  }, 1000);
}

async function talk() {
  if (pc) return;
  $("talk").disabled = true;
  const mic = await navigator.mediaDevices.getUserMedia({ audio: true });
  pc = new RTCPeerConnection();
  mic.getTracks().forEach((t) => pc.addTrack(t, mic));
  pc.addTransceiver("audio", { direction: "recvonly" });
  pc.addTransceiver("video", { direction: "recvonly" });
  pc.ontrack = (ev) => {
    const el = ev.track.kind === "video" ? $("video") : $("audio");
    el.srcObject = ev.streams[0] || new MediaStream([ev.track]);
    el.play().catch(() => {});
  };
  pc.onconnectionstatechange = () => log(`webrtc ${pc.connectionState}`);
  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  await new Promise((resolve) => {
    if (pc.iceGatheringState === "complete") return resolve();
    pc.onicegatheringstatechange = () => pc.iceGatheringState === "complete" && resolve();
    setTimeout(resolve, 1500);
  });
  const url = `${session.offer_url}?token=${session.control_token}&avatar=${$("avatar-on").checked}`;
  const res = await fetch(url, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ sdp: pc.localDescription.sdp, type: pc.localDescription.type }),
  });
  if (!res.ok) { log(`offer failed: ${res.status} ${await res.text()}`); pc.close(); pc = null; $("talk").disabled = false; return; }
  const answer = await res.json();
  await pc.setRemoteDescription({ sdp: answer.sdp, type: answer.type });
  log("media plane connected — speak");
}

render();
start();
$("talk").onclick = talk;
