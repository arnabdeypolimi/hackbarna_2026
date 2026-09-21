import { useEffect } from 'react';
import type { CommandArgsByVerb, CommandMsg, ScreenState } from '@contracts/protocol';
import type { Outbound } from '../lib/avatarClient';

/**
 * What the TV does for each verb. Return a string to ack `ok: false` with that reason —
 * it is what lets the agent say "there is no trailer for that one" instead of "done".
 */
export interface CommandHandler {
  play(args: CommandArgsByVerb['play']): string | void;
  pause(): string | void;
  resume(): string | void;
  seek(args: CommandArgsByVerb['seek']): string | void;
  navigate(args: CommandArgsByVerb['navigate']): string | void;
  focus(args: CommandArgsByVerb['focus']): string | void;
  open_details(args: CommandArgsByVerb['open_details']): string | void;
  close(): string | void;
  back(): string | void;
  home(): string | void;
  show_products(args: CommandArgsByVerb['show_products']): string | void;
  show_titles(args: CommandArgsByVerb['show_titles']): string | void;
  search_catalog(args: CommandArgsByVerb['search_catalog']): Array<{ title_id: string; name: string }>;
  show_ambient(args: CommandArgsByVerb['show_ambient']): string | void;
  hide_ambient(): string | void;
}

export type Send = (msg: Outbound) => void;

export function dispatchCommand(cmd: CommandMsg, handler: CommandHandler, send: Send): void {
  if (cmd.verb === 'search_catalog') {
    // The one verb the backend awaits (400 ms). Pure in-memory filtering, so the budget
    // is not in play, but it must be answered with a `result`, never an `ack` — and
    // answered even when the handler throws, or the turn sits out the whole budget.
    let titles: Array<{ title_id: string; name: string }> = [];
    try {
      titles = handler.search_catalog(cmd.args);
    } catch (e) {
      console.warn('[avatar] search_catalog failed', e);
    }
    send({ type: 'result', command_id: cmd.id, data: { titles } });
    console.debug('[avatar] command', cmd.verb, cmd.args, `${titles.length} titles`);
    return;
  }
  let error: string | null = null;
  try {
    // Only a string is a reason. A handler that hands back some library's return value
    // (YouTube's chainable player, say) must not turn into an ack the backend rejects.
    const out: unknown = (handler[cmd.verb] as (a: unknown) => unknown)(cmd.args);
    if (typeof out === 'string') error = out;
  } catch (e) {
    error = (e as Error).message;
  }
  send({ type: 'ack', command_id: cmd.id, ok: !error, error });
  console.debug('[avatar] command', cmd.verb, cmd.args, error ?? 'ok');
}

/**
 * Push the screen to the backend whenever it changes. Trailing-edge debounce: a
 * `navigate right 5` and a scrub both burst, and the backend only reads the latest at
 * turn start, so the intermediate states are waste. `state` must be memoised by the
 * caller or this fires every render.
 */
export function useScreenStatePush(state: ScreenState, send: Send, live: boolean): void {
  useEffect(() => {
    if (!live) return;
    const t = setTimeout(() => send({ type: 'screen_state', state }), 120);
    return () => clearTimeout(t);
  }, [state, send, live]);
}
