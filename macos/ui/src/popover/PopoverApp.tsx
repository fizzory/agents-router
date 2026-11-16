// The menu bar popover. Data arrives as native envelopes: usage-updated is fresh
// usage (bars may slide), popover-status only carries reading/error/update state.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { contentHeight, createHeightReporter, cssPanelMotion } from '../../logic/height.js';
import { lastRow, popoverModel, TITLE } from '../../logic/rows.js';
import {
  AccountRow, Button, GroupHeading, Icon, Menu, ROW_ACTION, ROW_LINK, TooltipProvider, TurningIcon,
  useReducedMotion, useRovingList, useTooltip, type MenuEntry, type RowModel,
} from '../shared';
import type { Envelope, PopoverApi } from './api';

interface Footer { kind: 'problem' | 'update'; text: string; detail: string; button: string; action: 'settings' | 'refresh' | 'update'; tooltip: string }
interface Model { rows: RowModel[]; header: string; reading: boolean; empty: boolean; first: boolean; footer: Footer | null }

export interface PopoverAppProps {
  api: PopoverApi;
  /** Unix seconds. Tests fix the clock and the time zone. */
  now?: () => number;
  timeZone?: string;
}

export function PopoverApp(props: PopoverAppProps) {
  return <TooltipProvider><Popover {...props} /></TooltipProvider>;
}

const LOADING = 'Loading accounts…';
const restart = (element: HTMLElement, name: string) => {
  element.classList.remove(name);
  void element.offsetWidth;
  element.classList.add(name);
};

function Popover({ api, now = () => Date.now() / 1000, timeZone }: PopoverAppProps) {
  const tooltip = useTooltip();
  const reduced = useReducedMotion();
  const [view, setView] = useState<{ snapshot: Envelope | null; animate: boolean }>({ snapshot: null, animate: false });
  const [problem, setProblem] = useState<string | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [active, setActive] = useState(false);
  const generation = useRef(-1);
  const freshGeneration = useRef(-1);
  const seenKeys = useRef<Set<string> | null>(null);
  const parts = {
    head: useRef<HTMLElement>(null), list: useRef<HTMLUListElement>(null), empty: useRef<HTMLDivElement>(null),
    loading: useRef<HTMLDivElement>(null), footer: useRef<HTMLElement>(null),
  };
  const more = useRef<HTMLButtonElement>(null);
  const report = useCallback((error: unknown) => setProblem(String(error)), []);

  // Newer generations only; a fresh event animates when the panel is showing.
  const accept = useCallback((next: Envelope, fresh: boolean) => {
    if (next.generation < generation.current) return;
    const animate = fresh && next.generation > freshGeneration.current && document.body.dataset.active === 'true';
    if (fresh) freshGeneration.current = Math.max(freshGeneration.current, next.generation);
    generation.current = next.generation;
    setProblem(null);
    setView({ snapshot: next, animate });
  }, []);

  const model = useMemo<Model | Error>(() => {
    try { return popoverModel(view.snapshot ?? {}, now(), { timeZone }) as Model; }
    catch (error) { return error instanceof Error ? error : new Error(String(error)); }
  }, [view.snapshot]);
  const shown: Model = model instanceof Error
    ? { rows: [], header: TITLE, reading: false, empty: false, first: false, footer: null } : model;
  const failure = problem ?? (model instanceof Error ? String(model) : null);
  const footer = (failure ? lastRow({ error: failure }) : shown.footer) as Footer | null;
  const reading = shown.reading;
  const animate = view.animate && active;

  const latest = useRef({ reading, menuOpen, api, report, tooltip });
  latest.current = { reading, menuOpen, api, report, tooltip };

  // Subscribe before the first read, and unsubscribe on unmount.
  useEffect(() => {
    let disposed = false;
    const unlisten: (() => void)[] = [];
    const keep = (stop: () => void) => (disposed ? stop() : unlisten.push(stop));
    (async () => {
      try {
        keep(await api.onUsageUpdated((value) => accept(value, true)));
        keep(await api.onPopoverStatus((value) => accept(value, false)));
        const first = await api.getSnapshot();
        if (!disposed) accept(first, false);
      } catch (error) {
        if (!disposed) report(error);
      }
    })();
    return () => {
      disposed = true;
      unlisten.splice(0).forEach((stop) => stop());
    };
  }, [api, accept, report]);

  // Height: the intrinsic content, reported to the native panel after commits.
  const reporter = useMemo(() => {
    const reducedMotion = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
    return createHeightReporter({
      measure: () => contentHeight(document.body, Object.values(parts).map((part) => part.current).filter((part): part is NonNullable<typeof part> => !!part),
        (part: Element) => getComputedStyle(part)),
      send: (size: { height: number; durationMs: number; curve: number[] }) => latest.current.api.setPopoverHeight(size),
      // Retained NSPanel pages can suspend animation frames while hidden; a
      // microtask still runs, including before the first show.
      schedule: (callback: () => void) => queueMicrotask(callback),
      reducedMotion,
      motion: () => cssPanelMotion(getComputedStyle(document.documentElement)),
      onError: (error: unknown) => latest.current.report(error),
    });
  }, []);
  useEffect(() => {
    const observer = new ResizeObserver(() => reporter.request());
    for (const part of Object.values(parts)) if (part.current) observer.observe(part.current);
    return () => observer.disconnect();
  }, [reporter]);
  useEffect(() => {
    reporter.state({ loading: shown.first });
    if (reduced && document.body.dataset.active === 'true') restart(document.body, 'content-change');
  }, [model]);

  // Panel shown or hidden: the retained page never unloads.
  useEffect(() => {
    const update = (value: boolean) => {
      document.body.dataset.active = String(value);
      setActive(value);
      if (value) {
        reporter.show();
        restart(document.body, 'panel-show');
      } else {
        latest.current.tooltip.hide();
        setMenuOpen(false);
        // Opening the popover again shows no focus ring; the first Tab lands on Refresh.
        if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
      }
    };
    const focus = () => update(true);
    const blur = () => update(false);
    const visibility = () => update(!document.hidden && document.hasFocus());
    window.addEventListener('focus', focus);
    window.addEventListener('blur', blur);
    document.addEventListener('visibilitychange', visibility);
    update(document.hasFocus());
    return () => {
      window.removeEventListener('focus', focus);
      window.removeEventListener('blur', blur);
      document.removeEventListener('visibilitychange', visibility);
    };
  }, [reporter]);

  const refresh = useCallback(() => {
    if (!latest.current.reading) latest.current.api.refreshUsage().catch(latest.current.report);
  }, []);
  const openSettings = useCallback(() => { latest.current.api.openSettings().catch(latest.current.report); }, []);
  const quit = useCallback(() => { latest.current.api.quitApp().catch(latest.current.report); }, []);

  // Escape and the ⌘ shortcuts listen on document: right after the panel opens
  // focus is on body, and the native smoke test dispatches on document.
  useEffect(() => {
    const keyDown = (event: KeyboardEvent) => {
      if (event.metaKey && !event.altKey && !event.ctrlKey) {
        const key = event.key.toLowerCase();
        const command = key === 'r' ? refresh : key === ',' ? openSettings : key === 'q' ? quit : null;
        if (command) { event.preventDefault(); command(); }
        return;
      }
      if (event.key !== 'Escape') return;
      event.preventDefault();
      if (latest.current.menuOpen) {
        setMenuOpen(false);
        more.current?.focus();
        return;
      }
      latest.current.tooltip.hide();
      latest.current.api.hidePopover().catch(latest.current.report);
    };
    document.addEventListener('keydown', keyDown);
    return () => document.removeEventListener('keydown', keyDown);
  }, [refresh, openSettings, quit]);

  const roving = useRovingList(shown.rows.map((row) => ({
    key: row.key, parts: row.action ? [ROW_LINK, ROW_ACTION] : [ROW_LINK], initial: row.action ? ROW_ACTION : ROW_LINK,
  })));

  // Rows new in a fresh render fade in, --stagger-step apart. Kept per snapshot,
  // so other renders don't cut the fade short.
  const entering = useMemo(() => {
    const keys = new Set(shown.rows.map((row) => row.key));
    const previous = seenKeys.current;
    seenKeys.current = keys;
    return view.animate && previous ? new Set([...keys].filter((key) => !previous.has(key))) : new Set<string>();
  }, [model]);

  const footerAction = () => {
    if (!footer) return;
    const call = footer.action === 'update' ? api.checkForUpdates() : footer.action === 'refresh' ? api.refreshUsage()
      : api.openSettings({ install: footer.action === 'settings' });
    call.catch(report);
  };

  const entries: MenuEntry[] = [
    { kind: 'item', id: 'check-updates', label: 'Check for Updates…', onSelect: () => { api.checkForUpdates().catch(report); } },
    { kind: 'separator' },
    { kind: 'item', id: 'quit', label: 'Quit Agents Router', shortcut: 'Meta+Q', shortcutLabel: '⌘Q', onSelect: quit },
  ];

  const status = footer?.kind === 'problem' ? [footer.text, footer.detail].filter(Boolean).join(' ')
    : shown.first ? LOADING : [shown.header === TITLE ? '' : shown.header, footer?.text ?? ''].filter(Boolean).join('. ');

  let index = -1;
  return (
    <>
      <header className="popover-head" ref={parts.head}>
        <h1 id="heading" className="popover-title">{shown.header}</h1>
        <Button variant="icon" id="refresh" aria-label={reading ? 'Refreshing usage' : 'Refresh usage'} aria-keyshortcuts="Meta+R"
          tooltip={reading ? 'Refreshing usage' : 'Refresh usage ⌘R'} aria-busy={reading} onClick={refresh}>
          <TurningIcon busy={reading} enabled={active && !reduced} />
        </Button>
        <Button variant="icon" id="settings" aria-label="Settings…" aria-keyshortcuts="Meta+Comma" tooltip="Settings ⌘," onClick={openSettings}>
          <Icon name="gear" />
        </Button>
        <Button variant="icon" id="more" ref={more} aria-label="More" aria-haspopup="menu" aria-expanded={menuOpen} aria-controls="app-menu"
          tooltip="More" onClick={() => { tooltip.hide(); setMenuOpen((open) => !open); }}>
          <Icon name="more" />
        </Button>
      </header>
      <Menu id="app-menu" className="app-menu" label="Agents Router" open={menuOpen} entries={entries} trigger={more}
        onClose={(focusTrigger) => { setMenuOpen(false); if (focusTrigger) more.current?.focus(); }} />
      <div className="account-scroll">
        <ul id="accounts" className="account-list" role="list" aria-label="Accounts" ref={parts.list}
          hidden={!shown.rows.length} onKeyDown={roving.onKeyDown}>
          {shown.rows.flatMap((row) => {
            index += 1;
            const item = (
              <AccountRow key={row.key} model={row} index={index} animate={animate} active={active} reduced={reduced}
                entering={animate && entering.has(row.key)}
                onOpen={(target) => { api.openSettings({ runtime: target.runtime, account: target.name }).catch(report); }}
                onAction={(target) => (target.action === 'login' ? api.loginAccount(target.runtime, target.name)
                  : api.checkAccount(target.runtime, target.name)).catch(report)}
                tabIndex={(part) => roving.tabIndex(row.key, part)} register={(part) => roving.register(row.key, part)}
                onFocusPart={(part) => roving.onFocus(row.key, part)} />
            );
            return row.providerFirst ? [<GroupHeading key={`heading:${row.runtime}`} as="li">{row.provider}</GroupHeading>, item] : [item];
          })}
        </ul>
      </div>
      <div className="empty-row" id="empty" ref={parts.empty} hidden={!shown.empty}>
        <span>No accounts yet.</span>
        <Button variant="inline" id="add" onClick={() => { api.openSettings({ account: '' }).catch(report); }}>Add account…</Button>
      </div>
      <div className="empty-row" id="reading" ref={parts.loading} hidden={!shown.first}>{LOADING}</div>
      <footer className="last-row" id="last-row" ref={parts.footer} hidden={!footer} data-tooltip={footer?.tooltip}>
        {footer?.kind === 'update' ? (
          <button type="button" className="update-action" onClick={footerAction}>
            <span className="update-dot" aria-hidden="true" />{footer.text}
          </button>
        ) : footer ? (
          <>
            <span className="footer-text">
              <span className="footer-message">{footer.text}</span>
              {footer.detail && <span className="footer-detail" id="footer-detail">{footer.detail}</span>}
            </span>
            <Button variant="inline" className="footer-button" aria-label={`${footer.text} ${footer.button}`}
              aria-describedby={footer.detail ? 'footer-detail' : undefined} onClick={footerAction}>{footer.button}</Button>
          </>
        ) : null}
      </footer>
      <div className="visually-hidden" id="status" role="status" aria-live="polite">{status}</div>
    </>
  );
}
