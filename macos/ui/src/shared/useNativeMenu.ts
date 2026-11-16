// Native context menus through show_pane_menu: a fresh decimal token per menu,
// fixed action names, and the pane-menu event runs the matching callback once.
import { useCallback, useEffect, useRef } from 'react';

export type PaneMenuEvent = { token: string; action: string };
type Unlisten = () => void;

export function useNativeMenu(show: (token: string, actions: string[]) => Promise<unknown>,
  subscribe: (handler: (event: PaneMenuEvent) => void) => Promise<Unlisten>, report: (error: unknown) => void) {
  const menu = useRef<{ token: string; actions: Record<string, () => unknown> } | null>(null);
  const serial = useRef(0);
  const latest = useRef({ show, report });
  latest.current = { show, report };
  useEffect(() => {
    let disposed = false;
    let stop: Unlisten | null = null;
    subscribe(({ token, action }) => {
      const current = menu.current;
      if (current?.token !== token) return;
      menu.current = null;
      const run = current.actions[action];
      if (run) Promise.resolve().then(run).catch(latest.current.report);
    }).then((unlisten) => { if (disposed) unlisten(); else stop = unlisten; }, latest.current.report);
    return () => { disposed = true; stop?.(); };
  }, [subscribe]);
  return useCallback((event: { preventDefault(): void } | null, actions: Record<string, () => unknown>) => {
    event?.preventDefault();
    const names = Object.keys(actions);
    if (!names.length) return;
    const token = String(++serial.current);
    menu.current = { token, actions };
    latest.current.show(token, names).catch(latest.current.report);
  }, []);
}
