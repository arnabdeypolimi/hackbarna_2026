// Mock TV app. Mirrors contracts/protocol.d.ts — regenerate that file
// with `uv run python tools/export_schemas.py` after any command change.
const V = 1;
const TITLES = [
  { title_id: "tt_1", name: "Heat" },
  { title_id: "tt_2", name: "Sicario" },
  { title_id: "tt_3", name: "Arrival" },
  { title_id: "tt_4", name: "Dune" },
];

let ws = null;
let focus = 0;
let playback = { state: "stopped", title_id: null, position_s: 0 };

const log = (m) => {
  const el = document.getElementById("log");
  el.textContent += m + "\n";
  el.scrollTop = el.scrollHeight;
};

function render() {
  document.getElementById("grid").innerHTML = TITLES.map(
    (t, i) =>
      `<div class="tile ${i === focus ? "focused" : ""}">${t.name}</div>`
  ).join("");
  document.getElementById("playback").textContent =
    playback.state === "stopped" ? "stopped" : `${playback.state} ${playback.title_id}`;
}

function pushScreenState() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({
    v: V,
    type: "screen_state",
    state: {
      view: playback.state === "playing" ? "player" : "grid",
      rail_id: "rail_mock",
      focus_index: focus,
      tiles: TITLES.map((t, i) => ({ ...t, position: i })),
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
    case "search_catalog":
      ws.send(JSON.stringify({ v: V, type: "result", command_id: msg.id,
        data: { titles: TITLES.slice(0, a.limit || 10) } }));
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

async function start() {
  const session = await (await fetch("/sessions", { method: "POST" })).json();
  log(`session ${session.session_id} (protocol v${session.protocol_version})`);
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(
    `${proto}://${location.host}${session.control_url}?token=${session.control_token}`
  );
  ws.onopen = () => { log("control socket open"); pushScreenState(); };
  ws.onclose = () => log("control socket closed");
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    log(`< ${ev.data}`);
    if (msg.type === "command") apply(msg);
    else if (msg.type === "agent_status")
      document.getElementById("status").textContent = msg.state;
  };
}

render();
start();
