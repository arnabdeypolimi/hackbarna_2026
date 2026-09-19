import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ChangeEvent } from 'react';
import type { Profile, Rect, Tab, Title } from './types/title';
import { DATA_URL, MAX_FILE_BYTES } from './config';
import { toTitles } from './lib/csv';
import { buildRow, TAB_TITLES } from './lib/rows';
import { readJSON, writeJSON } from './lib/storage';
import { catalogFor } from './lib/maturity';
import {
  dropProfileData, historyKey, listKey, loadActiveId, loadProfiles, refileByTitle, saveActiveId, saveProfiles,
} from './lib/profiles';
import { BACK_KEYS, KEY, THEME_KEYS, exitApp, initTTS, speak } from './lib/titan';
import {
  applyTheme, loadChoice, loadTunes, msToNextSeason, resolve, saveChoice, saveTunes, type ThemeChoice, type Tune, type Tunes,
} from './lib/theme';
import { DIRS, findNext, isVisible, type Dir } from './lib/spatialNav';
import { Stage } from './components/Stage';
import { SearchBar } from './components/SearchBar';
import { PosterRow } from './components/PosterRow';
import { Detail } from './components/Detail';
import { AvatarPanel } from './components/AvatarPanel';
import { TabBar } from './components/TabBar';
import { ExitDialog } from './components/ExitDialog';
import { Profiles } from './components/Profiles';
import { ThemePicker } from './components/ThemePicker';
import { SkyVideo } from './components/SkyVideo';
import { Toast, useToast } from './components/Toast';
import { useAvatar } from './hooks/useAvatar';
import { useScreenStatePush, type CommandHandler } from './hooks/useTvControl';
import { deriveScreenState, fromWireId, searchCatalog, STOPPED, toWireId, type PlaybackReport } from './lib/tvBridge';
import { TrailerPlayer, type TrailerPlayerHandle } from './components/TrailerPlayer';
import { UploadIcon } from './components/Icons';

/** What is on screen: the title, the box it grew out of, and whether it owns the whole stage. */
interface Playing { item: Title; from: Rect | null; full: boolean }

type Status =
  | { kind: 'loading' }
  | { kind: 'ready' }
  | { kind: 'missing' }
  | { kind: 'error'; message: string };

export default function App() {
  const [items, setItems] = useState<Title[]>([]);
  const [status, setStatus] = useState<Status>({ kind: 'loading' });
  const [tab, setTab] = useState<Tab>('popular');
  const [query, setQuery] = useState('');
  const [sel, setSel] = useState(0);
  const [profiles, setProfiles] = useState<Profile[]>(loadProfiles);
  const [activeId, setActiveId] = useState<string>(() => loadActiveId(profiles));
  const [myList, setMyList] = useState<string[]>(() => readJSON(listKey(activeId), []));
  // Write-only since the resume panel left: nothing renders history today, but Watch
  // keeps recording it because the agent will want it.
  const [, setHistory] = useState<Record<string, number>>(() => readJSON(historyKey(activeId), {}));
  const [dialogOpen, setDialogOpen] = useState(false);
  const [profilesOpen, setProfilesOpen] = useState(false);
  const [themeChoice, setThemeChoice] = useState<ThemeChoice>(loadChoice);
  const [clock, setClock] = useState(0); // bumped when the season may have changed
  const [themeOpen, setThemeOpen] = useState(false);
  const [tunes, setTunes] = useState<Tunes>(loadTunes);
  const [player, setPlayer] = useState<Playing | null>(null);
  const [playback, setPlayback] = useState<PlaybackReport>(STOPPED);
  const [dragging, setDragging] = useState(false);
  const toast = useToast();

  const stageRef = useRef<HTMLDivElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const profilesRef = useRef<HTMLDivElement>(null);
  const profilesBack = useRef<() => boolean>(() => false);
  const themesRef = useRef<HTMLDivElement>(null);
  const playerRef = useRef<HTMLDivElement>(null);
  const trailer = useRef<TrailerPlayerHandle>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const prevFocus = useRef<HTMLElement | null>(null);
  const wantRowFocus = useRef(false);
  const avatarVideo = useRef<HTMLVideoElement>(null);
  // Assigned below, once the actions it calls exist; useAvatar only reads it on a command.
  const tv = useRef<CommandHandler | null>(null);
  const avatar = useAvatar(avatarVideo, { userId: activeId, commands: tv });

  const profile = profiles.find((p) => p.id === activeId) || profiles[0];
  // A kids profile browses a filtered dataset, so every row, search and resume reads this.
  const catalog = useMemo(() => catalogFor(items, profile), [items, profile]);
  const row = useMemo(() => buildRow(catalog, tab, query, myList), [catalog, tab, query, myList]);
  const selIdx = Math.min(sel, Math.max(0, row.length - 1));
  const current: Title | undefined = row[selIdx];

  // ---------- theme ----------
  // `clock` moves once a day at most, so this settles on one of four objects.
  const theme = useMemo(() => resolve(themeChoice, new Date()), [themeChoice, clock]);
  // Before paint, so the room never shows a frame of the wrong sky.
  useLayoutEffect(() => { applyTheme(theme, tunes[theme.id]); }, [theme, tunes]);
  // On auto the room follows the calendar: wake when the season ends rather than polling.
  useEffect(() => {
    if (themeChoice !== 'auto') return;
    const tick = () => setClock((n) => n + 1);
    const id = window.setTimeout(tick, msToNextSeason(new Date()));
    // A set that slept through a boundary would otherwise come back on the wrong sky.
    const onVisible = () => { if (!document.hidden) tick(); };
    document.addEventListener('visibilitychange', onVisible);
    return () => { window.clearTimeout(id); document.removeEventListener('visibilitychange', onVisible); };
  }, [themeChoice, clock]);

  // ---------- focus helpers ----------
  // Focus the selected poster once the DOM reflects the latest state: after the next
  // render if one is pending, or on the next frame if nothing changed.
  const flushRowFocus = () => {
    if (!wantRowFocus.current) return;
    wantRowFocus.current = false;
    const stage = stageRef.current;
    const target =
      stage?.querySelector<HTMLElement>('.poster.sel') ||
      stage?.querySelector<HTMLElement>('.tab.cur') ||
      stage?.querySelector<HTMLElement>('.tab');
    target?.focus();
  };
  const focusRow = () => {
    wantRowFocus.current = true;
    requestAnimationFrame(flushRowFocus);
  };
  useEffect(flushRowFocus);

  const selectPoster = (n: number) => { setSel(n); focusRow(); };

  // With no dataset yet, start on the import button so the remote has somewhere to go.
  useEffect(() => {
    if (status.kind === 'missing') stageRef.current?.querySelector<HTMLElement>('[data-role="import-empty"]')?.focus();
  }, [status.kind]);

  // ---------- data ----------
  const applyData = (text: string, fileName?: string) => {
    try {
      const next = toTitles(text);
      setItems(next);
      setStatus({ kind: 'ready' });
      setTab('popular');
      setQuery('');
      setSel(0);
      if (fileName) toast.show(`Loaded ${next.length} titles from ${fileName}`);
      focusRow();
    } catch (err) {
      if (fileName) toast.show((err as Error).message, 'alert');
      else setStatus({ kind: 'error', message: (err as Error).message });
    }
  };

  useEffect(() => {
    let cancelled = false;
    fetch(DATA_URL)
      .then((r) => (r.ok ? r.text() : Promise.reject(new Error('missing'))))
      .then((text) => {
        if (cancelled) return;
        // Dev servers answer unknown paths with index.html, so treat HTML as "no file yet".
        if (text.trimStart().startsWith('<')) { setStatus({ kind: 'missing' }); return; }
        applyData(text);
      })
      .catch(() => { if (!cancelled) setStatus({ kind: 'missing' }); });
    initTTS();
    return () => { cancelled = true; };
  }, []);

  // Saved lists from before titles had ids can only be matched up once a dataset is here.
  useEffect(() => {
    if (!items.length) return;
    if (refileByTitle(profiles.map((p) => p.id), items).includes(activeId)) {
      setMyList(readJSON(listKey(activeId), []));
      setHistory(readJSON(historyKey(activeId), {}));
    }
  }, [items]);

  const readFile = (file: File | undefined) => {
    if (!file) return;
    if (file.size > MAX_FILE_BYTES) {
      toast.show('That file is over 50 MB. Trim it with scripts/trim_tmdb.py first.', 'alert');
      return;
    }
    const reader = new FileReader();
    reader.onload = () => applyData(String(reader.result), file.name);
    reader.readAsText(file);
  };
  const onFile = (e: ChangeEvent<HTMLInputElement>) => { readFile(e.target.files?.[0]); e.target.value = ''; };

  // ---------- actions ----------
  /**
   * Watch plays the title's trailer across the whole stage. It is the only footage the
   * product has, so the player labels it a trailer rather than pretending to be the film.
   */
  const watch = (item: Title) => {
    setHistory((h) => { const next = { ...h, [item.id]: Date.now() }; writeJSON(historyKey(activeId), next); return next; });
    avatar.send({ type: 'user_event', event: 'watch', detail: { title_id: toWireId(item) } });
    if (!item.trailerKey) { toast.show(`No trailer available for ${item.title}`, 'alert'); return; }
    prevFocus.current = document.activeElement as HTMLElement;
    // It grows out of the browse panel, so the film opens from where the viewer was looking.
    const stage = stageRef.current;
    const panel = stage?.querySelector<HTMLElement>('.main');
    setPlayer({
      item,
      full: true,
      from: stage && panel ? {
        left: panel.offsetLeft,
        top: panel.offsetTop,
        right: stage.clientWidth - panel.offsetLeft - panel.offsetWidth,
        bottom: stage.clientHeight - panel.offsetTop - panel.offsetHeight,
      } : null,
    });
  };

  const toggleSave = (item: Title) => {
    const had = myList.includes(item.id);
    const next = had ? myList.filter((k) => k !== item.id) : [...myList, item.id];
    setMyList(next);
    writeJSON(listKey(activeId), next);
    avatar.send({ type: 'user_event', event: had ? 'unsave' : 'save', detail: { title_id: toWireId(item) } });
    toast.show(had ? `Removed ${item.title} from My List` : `Saved ${item.title} to My List`);
    if (had && tab === 'list' && !query) focusRow();
  };

  const selectTab = (t: Tab) => { setTab(t); setQuery(''); setSel(0); };

  // ---------- profiles ----------
  const openProfiles = () => { prevFocus.current = document.activeElement as HTMLElement; setProfilesOpen(true); };
  const closeProfiles = () => {
    setProfilesOpen(false);
    const back = prevFocus.current;
    requestAnimationFrame(() => (back && document.contains(back) ? back.focus() : focusRow()));
  };

  /**
   * Hands the TV to one profile: its own lists swap in, and browsing starts over on
   * Recommended with no search and nothing the last viewer had playing.
   */
  const activate = (id: string) => {
    setActiveId(id);
    saveActiveId(id);
    setMyList(readJSON(listKey(id), []));
    setHistory(readJSON(historyKey(id), {}));
    setPlayer(null);
    setTab('popular');
    setQuery('');
    setSel(0);
  };

  const switchProfile = (id: string) => {
    const next = profiles.find((p) => p.id === id);
    if (!next) return;
    activate(id);
    setProfilesOpen(false);
    toast.show(next.kind === 'kids' ? `Watching as ${next.name} — kids titles only` : `Watching as ${next.name}`);
    focusRow();
  };

  const writeProfiles = (list: Profile[]) => { setProfiles(list); saveProfiles(list); };

  const removeProfile = (id: string) => {
    const left = profiles.filter((p) => p.id !== id);
    if (!left.length) return; // the last profile stays; the Delete button is hidden for it
    const gone = profiles.find((p) => p.id === id);
    writeProfiles(left);
    dropProfileData(id);
    if (id === activeId) activate(left[0].id);
    if (gone) toast.show(`Deleted ${gone.name}`, 'alert');
  };

  const pickTheme = (choice: ThemeChoice) => { setThemeChoice(choice); saveChoice(choice); };
  const tuneTheme = (tune: Tune) => { const next = { ...tunes, [theme.id]: tune }; setTunes(next); saveTunes(next); };
  const openThemes = () => { prevFocus.current = document.activeElement as HTMLElement; setThemeOpen(true); };
  const closeThemes = () => {
    setThemeOpen(false);
    const back = prevFocus.current;
    requestAnimationFrame(() => (back && document.contains(back) ? back.focus() : focusRow()));
  };

  const openTrailer = (item: Title) => {
    if (!item.trailerKey) { toast.show(`No trailer available for ${item.title}`, 'alert'); return; }
    prevFocus.current = document.activeElement as HTMLElement;
    // Offsets, not getBoundingClientRect: the stage is scaled, these stay in stage units.
    // Stored as insets so every edge animates to 0 and the box is pulled open by its
    // top-left corner, the bottom-right barely moving from where the tile already sat.
    const tile = stageRef.current?.querySelector<HTMLElement>('.trailer');
    const box = tile?.offsetParent as HTMLElement | null;
    setPlayer({
      item,
      full: false,
      from: tile && box ? {
        left: tile.offsetLeft,
        top: tile.offsetTop,
        right: box.clientWidth - tile.offsetLeft - tile.offsetWidth,
        bottom: box.clientHeight - tile.offsetTop - tile.offsetHeight,
      } : null,
    });
  };
  const closePlayer = () => {
    setPlayer(null);
    setPlayback(STOPPED);
    const prev = prevFocus.current;
    requestAnimationFrame(() => (prev && document.contains(prev) ? prev.focus() : focusRow()));
  };

  const openDialog = () => { prevFocus.current = document.activeElement as HTMLElement; setDialogOpen(true); };
  const closeDialog = () => {
    setDialogOpen(false);
    const back = prevFocus.current;
    requestAnimationFrame(() => (back && document.contains(back) ? back.focus() : focusRow()));
  };
  const exit = () => { if (!exitApp()) { closeDialog(); toast.show('Exit closes the app on the TV', 'alert'); } };

  const back = (inSearch: boolean) => {
    if (profilesOpen) { if (!profilesBack.current()) closeProfiles(); return; }
    if (themeOpen) return closeThemes();
    if (player) return closePlayer();
    if (dialogOpen) return closeDialog();
    if (inSearch || query) { setQuery(''); setSel(0); return focusRow(); }
    if (tab !== 'popular') { selectTab('popular'); return focusRow(); }
    openDialog();
  };

  // ---------- remote control ----------
  const move = (dir: Dir, active: HTMLElement | null) => {
    const scope = profilesOpen ? profilesRef.current
      : themeOpen ? themesRef.current
      : player ? playerRef.current
      : dialogOpen ? dialogRef.current
      : stageRef.current;
    if (!scope) return;
    if (!active || !scope.contains(active)) { scope.querySelector<HTMLElement>('.f')?.focus(); return; }

    // Inside the row, left/right steps through posters and slides the track.
    if (active.classList.contains('poster') && (dir === 'left' || dir === 'right')) {
      const n = selIdx + (dir === 'right' ? 1 : -1);
      if (n >= 0 && n < row.length) return selectPoster(n);
      if (dir === 'left') return;
    }

    // Only posters actually visible inside the row can be targets.
    const wrap = scope.querySelector('.rowwrap')?.getBoundingClientRect();
    const candidates = Array.from(scope.querySelectorAll<HTMLElement>('.f')).filter((el) => {
      if (el === active || !isVisible(el)) return false;
      if (!el.classList.contains('poster') || !wrap) return true;
      const r = el.getBoundingClientRect();
      return r.right > wrap.left + 20 && r.left < wrap.right - 20;
    });
    const next = findNext(active, dir, candidates);
    if (!next) return;
    // Entering the row always lands on the selected poster, not the nearest one.
    if (next.classList.contains('poster') && !active.classList.contains('poster')) return focusRow();
    next.focus();
  };

  const onKey = (e: KeyboardEvent) => {
    const active = document.activeElement as HTMLElement | null;
    const inSearch = active?.id === 'search-input';
    const inText = active?.tagName === 'INPUT';
    const dir = DIRS[e.keyCode];

    if (dir) {
      if (inText && (dir === 'left' || dir === 'right')) return; // move the text cursor
      // The scrubber owns left/right so it can seek instead of moving focus.
      if (active?.classList.contains('scrub') && (dir === 'left' || dir === 'right')) return;
      e.preventDefault();
      move(dir, active);
      return;
    }
    if (e.keyCode === KEY.ENTER && inSearch) { e.preventDefault(); move('down', active); return; }
    if (BACK_KEYS.includes(e.keyCode)) {
      // Backspace deletes text while a field still has some.
      if (inText && e.keyCode === 8 && (active as HTMLInputElement).value) return;
      e.preventDefault();
      back(inSearch);
      return;
    }
    if (dialogOpen || player || profilesOpen || themeOpen) return;
    if (e.keyCode === KEY.RED && current) toggleSave(current);
    else if (e.keyCode === KEY.YELLOW) fileRef.current?.click();
    // Not from the search box, where G is a letter the viewer is typing.
    else if (THEME_KEYS.includes(e.keyCode) && !inText) openThemes();
    else if ((e.keyCode === KEY.PLAY || e.keyCode === KEY.PLAY_PAUSE) && current) watch(current);
  };

  // ---------- the agent ----------
  // Bring a title on screen and focus it. Resolved against the whole catalogue, not the
  // visible row: a recommendation the agent just made may not be on the rail the viewer
  // is on, and the search rail is the app's only way to show an arbitrary title.
  const reveal = (title_id: string): string | void => {
    const t = fromWireId(title_id, catalog);
    if (!t) return `unknown title ${title_id}`;
    if (player) closePlayer();
    const at = row.indexOf(t);
    if (at >= 0) return selectPoster(at);
    setQuery(t.title);
    selectPoster(0);
  };
  // The remote's Back at the home screen asks about leaving the app; a spoken "back" with
  // nothing to go back from should not.
  const goBack = (): string | void => {
    if (!profilesOpen && !player && !dialogOpen && !query && tab === 'popular') return 'already at home';
    back(false);
  };

  tv.current = {
    play: ({ title_id }) => {
      const t = fromWireId(title_id, catalog);
      if (!t) return `unknown title ${title_id}`;
      if (!t.trailerKey) return `no trailer for ${t.title}`;
      if (player) closePlayer();
      watch(t);
    },
    pause: () => (player ? trailer.current?.pause() : 'nothing is playing'),
    resume: () => (player ? trailer.current?.resume() : 'nothing is playing'),
    seek: ({ to_seconds, delta_seconds }) => {
      if (!player) return 'nothing is playing';
      if (to_seconds != null) trailer.current?.seekTo(to_seconds);
      else if (delta_seconds != null) trailer.current?.seekBy(delta_seconds);
    },
    navigate: ({ direction, count }) => {
      // move() is synchronous DOM focus, so repeating it advances one step each time.
      for (let i = 0; i < (count ?? 1); i++) move(direction, document.activeElement as HTMLElement | null);
    },
    focus: ({ title_id }) => reveal(title_id),
    open_details: ({ title_id }) => reveal(title_id),
    close: goBack,
    back: goBack,
    home: () => { if (player) closePlayer(); selectTab('popular'); focusRow(); },
    show_products: () => 'not supported on this TV',
    search_catalog: ({ query: q, limit }) =>
      searchCatalog(catalog, q, limit ?? 10).map((t) => ({ title_id: toWireId(t), name: t.title })),
  };

  const screen = useMemo(
    () => deriveScreenState({ tab, query, row, selIdx, playing: player?.item ?? null, playback }),
    [tab, query, row, selIdx, player, playback],
  );
  useScreenStatePush(screen, avatar.send, avatar.phase === 'live');

  // One listener for the app's lifetime that always calls the latest handler.
  const keyHandler = useRef(onKey);
  keyHandler.current = onKey;
  useEffect(() => {
    const listener = (e: KeyboardEvent) => keyHandler.current(e);
    document.addEventListener('keydown', listener, true);
    const onFocus = (e: FocusEvent) => {
      const el = e.target as HTMLElement;
      if (el.classList?.contains('f')) speak(el.getAttribute('aria-label') || el.textContent?.trim() || '');
    };
    document.addEventListener('focusin', onFocus);
    return () => {
      document.removeEventListener('keydown', listener, true);
      document.removeEventListener('focusin', onFocus);
    };
  }, []);

  // Drag and drop a CSV anywhere (desktop testing).
  useEffect(() => {
    const over = (e: DragEvent) => { e.preventDefault(); setDragging(true); };
    const leave = (e: DragEvent) => { if (!e.relatedTarget) setDragging(false); };
    const drop = (e: DragEvent) => { e.preventDefault(); setDragging(false); readFile(e.dataTransfer?.files[0]); };
    window.addEventListener('dragover', over);
    window.addEventListener('dragleave', leave);
    window.addEventListener('drop', drop);
    return () => {
      window.removeEventListener('dragover', over);
      window.removeEventListener('dragleave', leave);
      window.removeEventListener('drop', drop);
    };
  }, []);

  // ---------- render ----------
  const heading = status.kind !== 'ready' ? 'Recommended' : query ? `Results for "${query}"` : TAB_TITLES[tab];

  const emptyRow =
    profile.kind === 'kids' && items.length > 0 && !catalog.length ? (
      <div className="empty">
        <b>Nothing here for kids yet</b>
        This dataset has no titles rated for children. Switch to an adult profile to browse it all.
      </div>
    ) : tab === 'list' && !query ? (
      <div className="empty"><b>Your list is empty</b>Press Save to My List on any title, or the red key, and it will show up here.</div>
    ) : (
      <div className="empty"><b>No matches</b>Try a shorter title or a genre like Drama.</div>
    );

  return (
    <>
      {/* Outside the stage so it fills the window, whatever shape a desktop gives it; the stage scales inside. */}
      {/* The loop waits until the titles are in, so their fetch and first paint come first. */}
      <div className="room"><SkyVideo theme={theme} paused={!!player || status.kind === 'loading'} enabled={tunes[theme.id].motion} /></div>
      <Stage ref={stageRef}>
        <SearchBar value={query} onChange={(v) => { setQuery(v); setSel(0); }} />
        <input ref={fileRef} type="file" accept=".csv,text/csv" hidden onChange={onFile} />

        <section className="panel main">
          <h1>{heading}{profile.kind === 'kids' && <span className="kidsflag">Kids</span>}</h1>
          {status.kind === 'ready' ? (
            <>
              <PosterRow items={row} sel={selIdx} saved={myList} empty={emptyRow} onPick={(i) => {
                if (i === selIdx) stageRef.current?.querySelector<HTMLElement>('[data-role="watch"]')?.focus();
                else selectPoster(i);
              }} />
              {current && (
                <Detail
                  item={current}
                  saved={myList.includes(current.id)}
                  onWatch={() => watch(current)}
                  onSave={() => toggleSave(current)}
                  onTrailer={() => openTrailer(current)}
                />
              )}
            </>
          ) : (
            <div className="empty data-state">
              {status.kind === 'loading' && <b>Loading titles…</b>}
              {status.kind === 'missing' && (
                <>
                  <b>No titles yet</b>
                  Add your dataset as public/data/titles.csv and reload, or load a CSV file now to try it.
                  <div className="actions">
                    <button className="btn f" data-role="import-empty" onClick={() => fileRef.current?.click()}>
                      <UploadIcon />Load titles
                    </button>
                  </div>
                </>
              )}
              {status.kind === 'error' && (<><b>Couldn't read titles.csv</b>{status.message}</>)}
            </div>
          )}
          {player && !player.full && (
            <TrailerPlayer
              ref={trailer} item={player.item} from={player.from} scopeRef={playerRef}
              onClose={closePlayer} onPlayback={setPlayback}
            />
          )}
        </section>

        <AvatarPanel view={avatar} videoRef={avatarVideo} />

        <TabBar tab={tab} highlight={!query} profile={profile} theme={theme} onSelect={selectTab} onProfile={openProfiles} onTheme={openThemes} />

        {player && player.full && (
          <TrailerPlayer
            ref={trailer} item={player.item} from={player.from} full scopeRef={playerRef}
            onClose={closePlayer} onPlayback={setPlayback}
          />
        )}

        <Toast message={toast.message} kind={toast.kind} visible={toast.visible} />
        {dragging && <div className="drop">Drop your CSV to load it</div>}
        <ExitDialog open={dialogOpen} scopeRef={dialogRef} onStay={closeDialog} onExit={exit} />
        <Profiles
          open={profilesOpen}
          profiles={profiles}
          activeId={activeId}
          scopeRef={profilesRef}
          backRef={profilesBack}
          onPick={switchProfile}
          onSave={writeProfiles}
          onDelete={removeProfile}
          onNotice={toast.show}
        />
        <ThemePicker
          open={themeOpen} choice={themeChoice} theme={theme} tune={tunes[theme.id]}
          scopeRef={themesRef} onPick={pickTheme} onTune={tuneTheme} onClose={closeThemes}
        />
      </Stage>
    </>
  );
}
