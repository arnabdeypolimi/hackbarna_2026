// Rehearses the judging scripts against the *real* stack — Vite TV app, backend, LLM,
// TTS — with two deliberate substitutions: the avatar is switched off (the offer URL
// is rewritten to avatar=false, so no Anam minutes), and the viewer's lines go in as
// text through POST /sessions/{id}/text, which lands where STT would have put them.
// Everything downstream of that — turn-taking, barge-in, the agent, commands, the UI —
// is the production path, and the assertions read the DOM the judges will see.
//
//   npm --prefix tools/rehearsal install && npm --prefix tools/rehearsal run setup
//   node tools/rehearsal/rehearse.mjs --script AB [--base http://127.0.0.1:5173]
//                                      [--name Arnab] [--user usr_...] [--headed]
//
// Exit status is the number of failed beats. Beats the code cannot pass yet are
// reported as such, not hidden.
import { chromium } from 'playwright';
import { writeFileSync } from 'node:fs';

const arg = (k, d) => { const i = process.argv.indexOf(k); return i > 0 ? process.argv[i + 1] : d; };
const flag = (k) => process.argv.includes(k);
const BASE = arg('--base', 'http://127.0.0.1:5173');
const SCRIPT = arg('--script', 'AB').toUpperCase();
const NAME = arg('--name', 'Arnab');
const FOLD_WAIT_S = Number(arg('--fold-wait', '25'));
const stamp = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14);
// A cold start needs a viewer the backend has never met; `--user` reuses one.
const USER = arg('--user', `usr_${NAME.toLowerCase()}_${stamp}`);

// The three demo profiles. Ids satisfy both the TV's `usr_` prefix and the backend's
// user_id pattern; only the one under test gets the fresh suffix.
const PROFILES = [
  { id: 'usr_igor_demo', name: 'Igor', color: '#3e3a36', kind: 'adult' },
  { id: 'usr_arik_demo', name: 'Arik', color: '#a8503c', kind: 'adult' },
  { id: 'usr_arnab_demo', name: 'Arnab', color: '#3f6157', kind: 'adult' },
].map((p) => (p.name === NAME ? { ...p, id: USER } : p));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// dispatchCommand logs the TV's reason on failure and the literal 'ok' otherwise.
const okay = (cmd) => !!cmd && (cmd.result == null || cmd.result === 'ok');
const results = [];
function beat(id, ok, detail, { note = false } = {}) {
  results.push({ id, ok, note, detail });
  const tag = note ? 'NOTE' : ok ? 'PASS' : 'FAIL';
  console.log(`${tag.padEnd(5)} ${id.padEnd(4)} ${detail}`);
}

async function main() {
  const browser = await chromium.launch({
    headless: !flag('--headed'),
    args: [
      '--use-fake-ui-for-media-stream',
      '--use-fake-device-for-media-stream',
      '--autoplay-policy=no-user-gesture-required',
    ],
  });
  const ctx = await browser.newContext({ permissions: ['microphone'], viewport: { width: 1920, height: 1080 } });
  const page = await ctx.newPage();
  await ctx.addInitScript(({ profiles, active }) => {
    localStorage.setItem('profiles', JSON.stringify(profiles));
    localStorage.setItem('activeProfile', JSON.stringify(active));
    localStorage.setItem('tv.avatar.language', JSON.stringify('en'));
    // With the avatar off the stream's video track never delivers a frame, and a
    // <video> element's play() promise waits for one forever — so the panel would
    // sit in "Connecting…". Status, captions and commands come over the control
    // socket, not the media, so resolving play() ourselves changes nothing we assert.
    const play = HTMLMediaElement.prototype.play;
    HTMLMediaElement.prototype.play = function () {
      const real = play.call(this).catch(() => {});
      return Promise.race([real, new Promise((r) => setTimeout(r, 500))]);
    };
  }, { profiles: PROFILES, active: USER });

  // No avatar: the pipeline runs STT → agent → TTS with nothing to lip-sync, and
  // nothing billed to Anam. `avatar=true` is hardcoded in avatarClient.ts, so this
  // is the one place to flip it without touching the app.
  await page.route('**/sessions/*/offer?**', (route) =>
    route.continue({ url: route.request().url().replace('avatar=true', 'avatar=false') }));

  // The TV app's own session is the one we speak into: capture its credentials.
  const state = { session: null, sessions: [], commands: [] };
  page.on('response', async (res) => {
    if (res.request().method() === 'POST' && /\/sessions$/.test(new URL(res.url()).pathname) && res.ok()) {
      state.session = await res.json();
      state.sessions.push(state.session);
    }
  });
  // dispatchCommand() logs every verb it executes and how the TV answered.
  page.on('console', async (msg) => {
    if (msg.type() !== 'debug') return;
    const args = await Promise.all(msg.args().map((a) => a.jsonValue().catch(() => String(a))));
    if (args[0] === '[avatar] command') state.commands.push({ verb: args[1], args: args[2], result: args[3], t: Date.now() });
  });

  const $ = {
    // Scoped: Detail.tsx has its own `.pill` ("Watch trailer") earlier in the DOM.
    status: () => page.locator('.avatarpanel .pill').getAttribute('data-state'),
    reply: () => page.locator('.say.reply span').textContent().then((s) => (s || '').trim()),
    heard: () => page.locator('.say.heard').textContent().then((s) => (s || '').trim()),
    heading: () => page.locator('.main h1').first().textContent().then((s) => (s || '').trim()),
    posters: () => page.locator('.poster').evaluateAll((els) => els.map((e) => e.getAttribute('aria-label').split(',')[0])),
    playerTitle: () => page.locator('.player h4').first().textContent({ timeout: 300 }).then((s) => (s || '').trim() || null).catch(() => null),
    avatarName: () => page.locator('.avatar-name').textContent().then((s) => (s || '').trim()).catch(() => ''),
  };

  async function until(pred, timeoutMs, every = 150) {
    const t0 = Date.now();
    while (Date.now() - t0 < timeoutMs) {
      const v = await pred().catch(() => null);
      if (v) return v;
      await sleep(every);
    }
    return null;
  }

  async function say(text) {
    const s = state.session;
    const res = await page.request.post(`${BASE}/sessions/${s.session_id}/text`, {
      headers: { 'X-Control-Token': s.control_token }, data: { text },
    });
    if (!res.ok()) throw new Error(`say(${JSON.stringify(text)}) -> ${res.status()} ${await res.text()}`);
    console.log(`\n  > ${text}`);
  }

  // A turn is over when the pill has been busy and is idle again with a fresh reply.
  async function turn(text, { timeoutMs = 45000 } = {}) {
    const before = { reply: await $.reply(), n: state.commands.length, t: Date.now() };
    await say(text);
    const busy = await until(async () => ['thinking', 'speaking'].includes(await $.status()), 15000);
    const done = await until(async () => {
      const s = await $.status();
      return (s === 'idle' || s === 'listening') && (await $.reply()) !== before.reply;
    }, timeoutMs);
    const reply = await $.reply();
    const commands = state.commands.slice(before.n);
    console.log(`  < ${reply || '(no reply)'}`);
    for (const c of commands) console.log(`    cmd ${c.verb} ${JSON.stringify(c.args)} -> ${c.result ?? 'ok'}`);
    return { reply, commands, busy: !!busy, done: !!done, ms: Date.now() - before.t };
  }

  async function connect(label) {
    state.session = null;
    await page.goto(BASE, { waitUntil: 'domcontentloaded' });
    const live = await until(() => page.locator('.face[data-state="live"]').count(), 30000);
    const face = await page.locator('.face-note').textContent().catch(() => '');
    if (!live) throw new Error(`${label}: avatar panel never went live (${face})`);
    if (!state.session) throw new Error(`${label}: no POST /sessions observed`);
    console.log(`\n== ${label}: session ${state.session.session_id} user ${state.session.user_id} lang ${state.session.language}`);
  }

  // The unprompted greeting is the first reply after connect; it arrives on its own.
  async function greeting(id, expect) {
    const reply = await until(async () => (await $.reply()) || null, 30000);
    const named = !!reply && new RegExp(`\\b${NAME}\\b`, 'i').test(reply);
    beat(id, !!reply, `greeting: ${JSON.stringify(reply)}`);
    beat(`${id}n`, named, named ? `greets by name` : `does not say "${NAME}" — greeting-by-name is not built (no name reaches the prompt)`, { note: !named });
    if (expect) beat(`${id}m`, expect.test(reply || ''), `mentions ${expect}: ${expect.test(reply || '')}`);
    // Let the greeting finish so A2 is a clean turn, not an accidental barge-in.
    await until(async () => (await $.status()) !== 'speaking', 20000);
  }

  // ---------------------------------------------------------------- Script A
  async function scriptA() {
    await connect('Script A');
    await greeting('A1');

    // A2: natural language, pill moves. Not awaited to the end on purpose — A3 cuts in.
    const t2 = Date.now();
    await say('Something tense. Long day.');
    const busy = await until(async () => ['thinking', 'speaking'].includes(await $.status()), 15000);
    beat('A2', !!busy, `pill left idle within ${Date.now() - t2} ms: ${!!busy}`);

    // A3: barge in while the avatar is mid-sentence. Text goes in as a multi-word
    // transcript, so the same two-word rule that protects against echo applies.
    const line = 'You pick. Crime and thrillers, no horror.';
    const speaking = await until(async () => (await $.status()) === 'speaking', 30000);
    const n3 = state.commands.length;
    let a3reply;
    if (!speaking) {
      const a3 = await turn(line);
      a3reply = a3.reply;
      beat('A3', false, `avatar never reached "speaking" to be interrupted; took the turn plainly, reply ${JSON.stringify(a3.reply)}`);
    } else {
      const interrupted = await $.reply();
      await sleep(600); // let a word or two out first, as a person would
      await say(line);
      const cut = await until(async () => (await $.status()) !== 'speaking', 4000);
      const heard = await until(async () => ((await $.heard()).includes('no horror') ? await $.heard() : null), 6000);
      const done = await until(async () => ['idle', 'listening'].includes(await $.status()) && (await $.reply()) !== interrupted, 45000);
      a3reply = await $.reply();
      console.log(`  (interrupted mid-reply: ${JSON.stringify(interrupted)})`);
      console.log(`  < ${a3reply}`);
      for (const c of state.commands.slice(n3)) console.log(`    cmd ${c.verb} ${JSON.stringify(c.args)} -> ${c.result ?? 'ok'}`);
      beat('A3', !!cut && !!heard && !!done, `cut off within 4 s: ${!!cut}; new turn heard: ${!!heard}; answered: ${!!done}`);
    }

    // A4: the rail rebuilt *for A3's constraint*, first poster focused, with a spoken reason.
    const after3 = state.commands.slice(n3);
    const shown = after3.filter((c) => c.verb === 'show_titles').pop();
    const autoplayed = after3.find((c) => c.verb === 'play');
    const posters = await $.posters();
    const heading = await $.heading();
    const selIdx = await page.locator('.poster.sel').getAttribute('data-poster').catch(() => null);
    beat('A4', !!shown && !autoplayed && posters.length >= 2 && selIdx === '0',
      `show_titles=${!!shown} autoplay=${!!autoplayed} heading=${JSON.stringify(heading)} rail=[${posters.slice(0, 5).join(' | ')}] focused=#${selIdx}`);
    beat('A4r', /\b(19|20)\d\d\b/.test(a3reply || ''), `spoken reason names a title and year: ${/\b(19|20)\d\d\b/.test(a3reply || '')}`, { note: true });

    // A5: "the second one" resolved from the live rail.
    const second = posters[1];
    const a5 = await turn('Play the second one.');
    const play = a5.commands.find((c) => c.verb === 'play');
    const playing = await until($.playerTitle, 8000);
    beat('A5', okay(play) && !!playing && playing === second,
      `play=${!!play}${play ? ` (${play.result ?? 'ok'})` : ''} player shows ${JSON.stringify(playing)} expected ${JSON.stringify(second)}`);

    // A6: transport by voice.
    const a6a = await turn('Pause.');
    const paused = a6a.commands.find((c) => c.verb === 'pause');
    const showsPlay = await until(() => page.locator('.player .ctl[aria-label="Play"]').count(), 5000);
    beat('A6a', okay(paused) && !!showsPlay, `pause=${!!paused} (${paused?.result ?? 'ok'}) player shows Play button: ${!!showsPlay}`);
    const a6b = await turn('Skip ahead thirty seconds.');
    const seek = a6b.commands.find((c) => c.verb === 'seek');
    beat('A6b', okay(seek) && seek.args?.delta_seconds === 30,
      `seek=${!!seek} args=${JSON.stringify(seek?.args)} (${seek?.result ?? 'ok'}) — "skip ahead" must be a relative jump`);

    // Ending the session is what writes the memory profile. A reload runs the same
    // teardown as switching profile: beforeunload → DELETE → pipeline cancel →
    // finish_session. The fold is an LLM call, so give it a moment before B.
    console.log(`\n== ending Script A session; waiting ${FOLD_WAIT_S} s for the memory fold`);
    await page.goto('about:blank');
    await sleep(FOLD_WAIT_S * 1000);
  }

  // ---------------------------------------------------------------- Script B
  async function scriptB() {
    await connect('Script B');
    await greeting('B1', /welcome back|again|last time|carry on|continue/i);

    const b2 = await turn('No, not that one. Something lighter.');
    const shown = b2.commands.find((c) => c.verb === 'show_titles');
    const posters = await $.posters();
    beat('B2', !!shown && posters.length >= 1, `rail rebuilt=${!!shown} rail=[${posters.slice(0, 5).join(' | ')}]`);
    beat('B3', posters.length >= 3, `${posters.length} posters on the new rail`);

    const b4 = await turn("What did I tell you I don't like?");
    beat('B4', /horror/i.test(b4.reply), `recalls horror: ${/horror/i.test(b4.reply)} — ${JSON.stringify(b4.reply)}`);

    const before = { sessions: state.sessions.length, name: await $.avatarName() };
    const b5 = await turn("Parla'm en català.", { timeoutMs: 30000 });
    const switched = state.sessions.length > before.sessions && state.sessions.at(-1).language === 'ca';
    beat('B5', switched,
      switched ? `TV re-connected in Catalan (${state.sessions.at(-1).avatar})`
        : `no language switch by voice — no set_language verb exists; the model answered ${JSON.stringify(b5.reply)} (session still ${state.session.language})`,
      { note: !switched });

    const b6 = await turn("What's that jacket he's wearing?");
    const shelf = await until(() => page.locator('.shelf').count(), 6000);
    const products = b6.commands.find((c) => c.verb === 'show_products');
    beat('B6', !!products && !!shelf, `show_products=${!!products}${products ? ` (${products.result ?? 'ok'})` : ''} shelf visible=${!!shelf}`, { note: !products });
  }

  try {
    if (SCRIPT.includes('A')) await scriptA();
    if (SCRIPT.includes('B')) await scriptB();
  } catch (err) {
    beat('RUN', false, `aborted: ${err.message}`);
  } finally {
    await page.goto('about:blank').catch(() => {});
    await browser.close();
  }

  const failed = results.filter((r) => !r.ok && !r.note).length;
  const out = `tools/rehearsal/last-run.json`;
  writeFileSync(out, JSON.stringify({ base: BASE, script: SCRIPT, user: USER, at: new Date().toISOString(), results, commands: state.commands }, null, 2));
  console.log(`\n${results.length - failed}/${results.length} beats ok, ${failed} failed. user=${USER}. Details: ${out}`);
  process.exit(failed);
}

main();
