// The menu bar popover. Data arrives as native envelopes: usage-updated is fresh
// usage (bars may slide), popover-status only carries reading/error state, and
// update-status is the updater's state, shared with Settings.
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { contentHeight, createHeightReporter, cssPanelMotion } from '../../logic/height.js';
import { lastRow, popoverModel, TITLE } from '../../logic/rows.js';
import {
  AccountRow, AnnounceProvider, Button, cssTime, GroupHeading, Icon, ROW_ACTION, ROW_COPY, ROW_LINK, Spinner, TooltipProvider, TurningIcon,
  useAnnounce, useReducedMotion, useRovingList, useTooltip, type AccountCheck, type RowModel, type UpdateStatus,
} from '../shared';
import type { Envelope, PopoverApi } from './api';

interface Footer { kind: 'problem'; text: string; detail: string; button: string; action: 'settings' | 'refresh'; tooltip: string }
interface UpdateRow { kind: 'available' | 'installing' | 'failed'; text: string; button: string; action: 'install' | 'releases' | null; detail: string; progress: string }
interface Model { rows: RowModel[]; header: string; freshness: string; reading: boolean; empty: boolean; first: boolean; footer: Footer | null; update: UpdateRow | null }

export interface PopoverAppProps {
  api: PopoverApi;
  /** Unix seconds. Tests fix the clock and the time zone. */
  now?: () => number;
  timeZone?: string;
}

export function PopoverApp(props: PopoverAppProps) {
  return <AnnounceProvider><TooltipProvider><Popover {...props} /></TooltipProvider></AnnounceProvider>;
}

const LOADING = 'Loading accounts…';
const currentTime = () => Date.now() / 1000;
const restart = (element: HTMLElement, name: string) => {
  element.classList.remove(name);
  void element.offsetWidth;
  element.classList.add(name);
};

function Popover({ api, now = currentTime, timeZone }: PopoverAppProps) {
  const announce = useAnnounce();
  const tooltip = useTooltip();
  const reduced = useReducedMotion();
  const [view, setView] = useState<{ snapshot: Envelope | null; animate: boolean }>({ snapshot: null, animate: false });
  const [checks, setChecks] = useState<AccountCheck[]>([]);
  const [problem, setProblem] = useState<string | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [active, setActive] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [loginFailures, setLoginFailures] = useState<Record<string, string>>({});
  const [loginRejections, setLoginRejections] = useState<Record<string, string>>({});
  const [refreshed, setRefreshed] = useState(false);
  const [updateStatus, setUpdateStatus] = useState<UpdateStatus | null>(null);
  // Installing… from the click until Rust's next update status.
  const [starting, setStarting] = useState(false);
  const installClick = useRef(0);
  const installing = useRef(0);
  const wasReading = useRef(false);
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const generation = useRef(-1);
  const freshGeneration = useRef(-1);
  const seenKeys = useRef<Set<string> | null>(null);
  const entryCycle = useRef(0);
  const parts = {
    head: useRef<HTMLElement>(null), list: useRef<HTMLUListElement>(null), empty: useRef<HTMLDivElement>(null),
    loading: useRef<HTMLDivElement>(null), trouble: useRef<HTMLDivElement>(null), footer: useRef<HTMLElement>(null),
  };
  const more = useRef<HTMLButtonElement>(null);
  const menuPending = useRef(false);
  const report = useCallback((error: unknown) => setProblem(String(error)), []);

  // Newer generations only; a fresh event animates when the panel is showing.
  const accept = useCallback((next: Envelope, fresh: boolean) => {
    if (next.generation < generation.current) return;
    const animate = fresh && next.generation > freshGeneration.current && document.body.dataset.active === 'true';
    if (fresh) freshGeneration.current = Math.max(freshGeneration.current, next.generation);
    generation.current = next.generation;
    setProblem(null);
    setView((previous) => ({ snapshot: next.error && !next.usage && previous.snapshot?.usage
      ? { ...next, usage: previous.snapshot.usage, fetched_at: previous.snapshot.fetched_at } : next, animate }));
  }, []);

  const model = useMemo<Model | Error>(() => {
    const update = starting && updateStatus ? { ...updateStatus, phase: 'installing' as const } : updateStatus;
    try { return popoverModel({ ...view.snapshot, account_checks: checks, update, ...(problem ? { error: problem } : {}) }, now(), { timeZone }) as Model; }
    catch (error) { return error instanceof Error ? error : new Error(String(error)); }
  }, [view.snapshot, checks, problem, updateStatus, starting, now, timeZone]);
  const shown: Model = model instanceof Error
    ? { rows: [], header: "Can't read agt", freshness: '', reading: false, empty: false, first: false, footer: null, update: null } : model;
  const trouble = (model instanceof Error ? lastRow({ error: String(model) }) : shown.footer) as Footer | null;
  const update = shown.update;
  const reading = shown.reading;
  const animate = view.animate && active;

  useEffect(() => {
    clearTimeout(refreshTimer.current);
    if (reading) setRefreshed(false);
    else if (wasReading.current && !trouble && document.body.dataset.active === 'true') setRefreshed(true);
    if (!reading && !trouble && wasReading.current) {
      refreshTimer.current = setTimeout(() => setRefreshed(false), cssTime('--copied-delay'));
    }
    if (trouble) setRefreshed(false);
    wasReading.current = reading;
    return () => clearTimeout(refreshTimer.current);
  }, [reading, !!trouble]);

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
        let checksReceived = false;
        keep(await api.onAccountChecksStatus((value) => { checksReceived = true; if (!disposed) setChecks(value); }));
        const checks = await api.getAccountChecks();
        if (!disposed && !checksReceived) setChecks(checks);
        const first = await api.getSnapshot();
        if (!disposed) accept(first, false);
      } catch (error) {
        if (!disposed) report(error);
      }
    })();
    (async () => {
      let received = false;
      const receive = (value: UpdateStatus) => {
        received = true;
        if (disposed) return;
        // A download or install step keeps the click's guard; any other state frees it.
        if (value.phase !== 'downloading' && value.phase !== 'installing') installing.current = 0;
        setStarting(false);
        setUpdateStatus(value);
      };
      try {
        keep(await api.onUpdateStatus(receive));
        const first = await api.getUpdateStatus();
        if (!disposed && !received) setUpdateStatus(first);
      } catch {
        // An unreadable update status hides only the update row; usage still reads.
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
  }, [model, expanded]);

  // Panel shown or hidden: the retained page never unloads.
  useEffect(() => {
    const update = (value: boolean) => {
      document.body.dataset.active = String(value);
      setActive(value);
      if (value) {
        reporter.show();
        restart(document.body, 'panel-show');
      } else {
        entryCycle.current += 1;
        latest.current.tooltip.hide();
        setExpanded(null);
        setLoginRejections({});
        setRefreshed(false);
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
        // AppKit handles Escape while tracking. Never turn a menu dismissal
        // into a hide request if a key is also delivered to the webview.
        return;
      }
      latest.current.tooltip.hide();
      latest.current.api.hidePopover().catch(latest.current.report);
    };
    document.addEventListener('keydown', keyDown);
    return () => document.removeEventListener('keydown', keyDown);
  }, [refresh, openSettings, quit]);

  const roving = useRovingList(shown.rows.map((row) => ({
    key: row.key, parts: row.action === 'login' ? [ROW_LINK, ROW_ACTION, ROW_COPY] : row.action ? [ROW_LINK, ROW_ACTION] : [ROW_LINK],
    initial: row.action ? ROW_ACTION : ROW_LINK,
  })));

  const accountAction = async (target: RowModel) => {
    if (target.action === 'retry') {
      // Rust publishes a retained failure for both process and provider errors.
      // A row check failure must not become a usage-read trouble row.
      await api.checkAccount(target.runtime, target.name).catch(() => {});
      return;
    }
    setLoginRejections((previous) => {
      const next = { ...previous };
      delete next[target.key];
      return next;
    });
    const cycle = entryCycle.current;
    try {
      const result = await api.loginAccount(target.runtime, target.name);
      setLoginFailures((previous) => {
        const next = { ...previous };
        if (result.status === 'terminal_failed') next[target.key] = result.copy_command;
        else delete next[target.key];
        return next;
      });
      if (result.status === 'terminal_failed' && document.body.dataset.active === 'true') setExpanded(target.key);
    } catch (error) {
      // A rejected handoff belongs to its row, and must not reopen a panel
      // hidden while the IPC request was pending (including a close/reopen).
      if (cycle === entryCycle.current && document.body.dataset.active === 'true' && seenKeys.current?.has(target.key)) {
        setLoginRejections((previous) => ({ ...previous, [target.key]: String(error) }));
        setExpanded(target.key);
        announce("Couldn't start login.");
      }
    }
  };

  // Only newly appearing keys enter. Commit the remembered keys after render,
  // so StrictMode's second render sees the same additions as the first.
  const entering = useMemo(() => {
    const keys = new Set(shown.rows.map((row) => row.key));
    const previous = seenKeys.current;
    return { cycle: entryCycle.current, keys: active && previous
      ? new Map([...keys].filter((key) => !previous.has(key)).map((key, index) => [key, Math.min(index, 7)])) : new Map<string, number>() };
  }, [model]);
  useLayoutEffect(() => { seenKeys.current = new Set(shown.rows.map((row) => row.key)); }, [model]);
  useEffect(() => {
    const keys = new Set(shown.rows.map((row) => row.key));
    const keep = (previous: Record<string, string>) => Object.keys(previous).some((key) => !keys.has(key))
      ? Object.fromEntries(Object.entries(previous).filter(([key]) => keys.has(key))) : previous;
    setLoginRejections(keep);
    setLoginFailures(keep);
  }, [model]);

  const troubleAction = () => {
    if (!trouble) return;
    const call = trouble.action === 'refresh' ? api.refreshUsage() : api.openSettings({ install: trouble.action === 'settings' });
    call.catch(report);
  };

  // Install and Restart, and Retry: one click, one install. Rust's shared guard
  // also turns away a click while Settings installs. Never opens Settings.
  const install = () => {
    if (installing.current) return;
    const click = ++installClick.current;
    installing.current = click;
    setStarting(true);
    const settle = () => {
      if (installing.current !== click) return;
      installing.current = 0;
      setStarting(false);
    };
    api.installUpdate().then(settle, settle);
  };
  const updateAction = () => {
    if (update?.action === 'install') install();
    else if (update?.action === 'releases') api.openReleases().catch(() => {});
  };

  const openMore = async () => {
    if (menuPending.current || !more.current) return;
    const rect = more.current.getBoundingClientRect();
    tooltip.hide();
    menuPending.current = true;
    setMenuOpen(true);
    try {
      await api.showPopoverMenu({ x: rect.left, y: rect.bottom });
    } catch {
      // A native menu failure is local to More; usage is still readable.
    } finally {
      menuPending.current = false;
      setMenuOpen(false);
      more.current?.focus();
    }
  };

  const status = trouble ? [trouble.text, trouble.detail].filter(Boolean).join('. ')
    : shown.first ? LOADING : [shown.header === TITLE ? '' : shown.header, update?.text ?? ''].filter(Boolean).join('. ');

  return (
    <>
      <header className="popover-head" ref={parts.head}>
        <h1 id="heading" className="popover-title">{shown.header}</h1>
        <Button variant="icon" id="refresh" aria-label={reading ? 'Refreshing usage' : 'Refresh usage'} aria-keyshortcuts="Meta+R"
          tooltip={['Refresh ⌘R', shown.freshness].filter(Boolean).join(' · ')} aria-busy={reading} onClick={refresh}>
          <span className="refresh-feedback" data-checked={refreshed}>
            <span className="refresh-glyph"><TurningIcon busy={reading} enabled={active && !reduced && !refreshed} /></span>
            <span className="refresh-check"><Icon name="check" /></span>
          </span>
        </Button>
        <Button variant="icon" id="settings" aria-label="Settings…" aria-keyshortcuts="Meta+Comma" tooltip="Settings ⌘," onClick={openSettings}>
          <Icon name="gear" />
        </Button>
        <Button variant="icon" id="more" ref={more} aria-label="More" aria-haspopup="menu" aria-expanded={menuOpen}
          tooltip="More" onClick={openMore}>
          <Icon name="more" />
        </Button>
      </header>
      <div className="trouble-row" id="trouble-row" ref={parts.trouble} hidden={!trouble}>
        {trouble && <>
          <span key={trouble.detail} className="trouble-detail" id="trouble-detail" role="alert">{trouble.detail}</span>
          <Button variant="text" className="trouble-action" busy={reading && trouble.action === 'refresh'}
            aria-label={reading && trouble.action === 'refresh' ? 'Retrying…' : trouble.button} aria-describedby="trouble-detail" onClick={troubleAction}>
            {reading && trouble.action === 'refresh' ? <>{!reduced && <Spinner enabled={active} />}Retrying…</> : trouble.button}
          </Button>
        </>}
      </div>
      <div className="account-scroll">
        <ul id="accounts" className="account-list" role="list" aria-label="Accounts" ref={parts.list}
          hidden={!shown.rows.length} onKeyDown={roving.onKeyDown}>
          {shown.rows.flatMap((row) => {
            const item = (
              <AccountRow key={row.key} model={row} index={entering.keys.get(row.key) ?? 0} animate={animate} active={active} reduced={reduced}
                entering={active && entering.cycle === entryCycle.current && entering.keys.has(row.key)} expanded={expanded === row.key}
                onOpen={(target) => setExpanded((current) => current === target.key ? null : target.key)}
                onSettings={(target) => { api.openSettings({ runtime: target.runtime, account: target.name }).catch(report); }}
                onAction={accountAction}
                loginCommand={{ get: () => api.getLoginCommand(row.runtime, row.name), copy: api.copyText,
                  failed: loginFailures[row.key], rejected: loginRejections[row.key] }}
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
      <footer className="last-row" id="last-row" ref={parts.footer} hidden={!update} data-kind={update?.kind}
        data-tooltip={update?.kind === 'failed' && update.detail ? update.detail : undefined}>
        {update?.kind === 'available' && <span className="update-dot" aria-hidden="true" />}
        {update?.kind === 'installing' && !reduced && <Spinner enabled={active} />}
        {update && <span className="update-text">{update.text}</span>}
        {update?.kind === 'installing' && <span className="update-progress">{update.progress}</span>}
        {update?.button && (
          <Button variant="inline" id="update-action" aria-describedby={update.kind === 'failed' && update.detail ? 'update-detail' : undefined}
            onClick={updateAction}>{update.button}</Button>
        )}
        {update?.kind === 'failed' && update.detail && <span id="update-detail" hidden>{update.detail}</span>}
      </footer>
      <div className="visually-hidden" id="status" role="status" aria-live="polite">{status}</div>
    </>
  );
}
