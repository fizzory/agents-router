import type { AccountCheck } from '../shared/types';
// The Settings window: sidebar, toolbar with the scope popup, the selected
// pane only, and one sheet host. Every write goes through one busy guard;
// config writes and role edits join the serialized session history.
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type MouseEvent } from 'react';
import { SettingsHistory } from '../../logic/settings-history.js';
import { isTextField } from '../../logic/settings.js';
import {
  AnnounceProvider, FlashContext, ScopePopup, SearchField, SheetHost, SidebarNav, TooltipProvider, useAnnounce, useNativeMenu,
  useReducedMotion, useTooltip, type FlashTarget, type NavItem, type RowMessage, type Scope,
} from '../shared';
import { installState } from '../../logic/settings.js';
import type { ConfigShow, Envelope, InstallStatus, RefreshKind, SettingsApi, SettingsTarget, UpdateStatus, Unlisten } from './api';
import { SettingsContext, refusal, type SettingsServices } from './context';
import {
  accountModels, errorMessage, isPane, PANE_IDS, PANE_TITLES, paneCount, paneFor, paneForKey, PROVIDER_PANES, rowState, settingName,
  testBuildRows, type ActivityEntry, type PaneId, type RolesDocument, type SharingData, type TestRow,
} from './model';
import { SettingsReads, type ReadOptions } from './reads';
import { ActivityPane } from './panes/ActivityPane';
import { AccountsPane } from './panes/AccountsPane';
import { GeneralPane, type LoginItem } from './panes/GeneralPane';
import { ProviderPane } from './panes/ProviderPane';
import { RolesPane } from './panes/RolesPane';
import { SharingPane } from './panes/SharingPane';
import { AddAccountSheet, InstallerSheet, UpdaterSheet } from './sheets';

export interface SettingsAppProps {
  api: SettingsApi;
  /** Unix milliseconds. Tests fix the clock and the time zone. */
  now?: () => number;
  timeZone?: string;
}

export function SettingsApp(props: SettingsAppProps) {
  return <TooltipProvider><AnnounceProvider><Settings {...props} /></AnnounceProvider></TooltipProvider>;
}

interface PaneData { roles: Map<string | null, RolesDocument>; accounts?: unknown; sharing?: Omit<SharingData, 'config'>; activity?: { entries?: ActivityEntry[]; more?: boolean } }
interface PaneReadOptions extends ReadOptions { force?: boolean; workspace?: string | null; configRequested?: boolean }
type HistoryStep = { key: string };
const DATA_PANES: PaneId[] = ['roles', 'accounts', 'sharing', 'activity'];
const STALE = 'Changed elsewhere. Showing the current value.';
const SCOPE_PANES: PaneId[] = ['general', 'roles', ...PROVIDER_PANES];
/** How long refresh events are collected into one batch. Tests advance a fake timer by it. */
export const SETTINGS_REFRESH_DELAY_MS = 50;
type ShareStatusResult = { value: unknown } | { error: unknown };
/** The read slot of a data pane, and the key a dropped pass marks dirty. */
const paneKey = (target: PaneId, workspace: string | null) => (target === 'roles' ? `roles:${workspace ?? ''}` : target);

function Settings({ api, now = () => Date.now(), timeZone }: SettingsAppProps) {
  const announce = useAnnounce();
  const tooltip = useTooltip();
  const reduced = useReducedMotion();
  const [pane, setPane] = useState<PaneId>('general');
  const [scope, setScope] = useState<Scope>({ level: 'user', workspace: null });
  const [folders, setFolders] = useState<string[]>([]);
  const [query, setQuery] = useState('');
  const [configs, setConfigs] = useState(() => new Map<string | null, ConfigShow>());
  const [configError, setConfigError] = useState<string | null>(null);
  const [snapshot, setSnapshot] = useState<Envelope | null>(null);
  const [checks, setChecks] = useState<AccountCheck[]>([]);
  /** The snapshot came from a newer usage-updated: Accounts slides its bars and rolls its digits, as the popover does. */
  const [freshUsage, setFreshUsage] = useState(false);
  const freshGeneration = useRef(-1);
  const [install, setInstall] = useState<InstallStatus | null>(null);
  const [installError, setInstallError] = useState<string | null>(null);
  const [update, setUpdate] = useState<UpdateStatus | null>(null);
  const [data, setData] = useState<PaneData>(() => ({ roles: new Map() }));
  const [paneErrors, setPaneErrors] = useState<Partial<Record<PaneId, string>>>({});
  const [messages, setMessages] = useState(() => new Map<string, RowMessage>());
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [flash, setFlash] = useState<FlashTarget | null>(null);
  const [attention, setAttention] = useState(false);
  const [login, setLogin] = useState<LoginItem>({ value: null, readable: true, busy: false });
  const [testRows, setTestRows] = useState<TestRow[]>([]);
  const [addAccount, setAddAccount] = useState(false);
  const [status, setStatus] = useState('Loading settings…');
  const search = useRef<HTMLInputElement>(null);
  const busy = useRef<string | null>(null);
  const flashSerial = useRef(0);
  const reads = useMemo(() => new SettingsReads(() => api.settingsVisible(), () => !!busy.current), [api]);
  const shareStatus = useRef<ShareStatusResult | null>(null);
  const [userConfigError, setUserConfigError] = useState<string | null>(null);
  const lastResult = useRef(new Map<string, unknown>());
  const preloaded = useRef(false);

  const report = useCallback((error: unknown) => setStatus(errorMessage(error)), []);
  const setMessage = useCallback((key: string, message: RowMessage | null) => setMessages((current) => {
    if (!message && !current.has(key)) return current;
    const next = new Map(current);
    if (message) next.set(key, message); else next.delete(key);
    return next;
  }), []);

  const state = { pane, scope, data, configs };
  const latest = useRef(state);
  latest.current = state;

  // body[data-pane] is a contract for the native smoke and the self-test; set only this one property.
  useLayoutEffect(() => {
    document.body.dataset.pane = pane;
    document.title = PANE_TITLES[pane];
  }, [pane]);

  const loadConfig = useCallback((workspace: string | null, options: ReadOptions = {}) =>
    reads.request(`config:${workspace ?? ''}`, async () => {
      try {
        const value = await api.configShow(workspace);
        setConfigs((current) => new Map(current).set(workspace, value));
        if (workspace === null) setUserConfigError(null);
        if (latest.current.scope.workspace === workspace) { setConfigError(null); setStatus(''); }
      } catch (error) {
        if (workspace === null) setUserConfigError(errorMessage(error));
        if (latest.current.scope.workspace === workspace) { setConfigError(errorMessage(error)); report(error); }
      }
    }, options), [api, reads, report]);

  // The badge and the Sharing pane share one status read and its result. Two
  // at once would have the app answer the older one "A newer read is running".
  const loadShareStatus = useCallback((options: ReadOptions = {}) => reads.request('share-status', async () => {
    try {
      const value = await api.shareStatus();
      shareStatus.current = { value };
      setAttention(!!(value as { attention?: unknown[] } | null)?.attention?.length);
      setData((previous) => (previous.sharing ? { ...previous, sharing: { ...previous.sharing, status: value } } : previous));
    } catch (error) { shareStatus.current = { error }; /* The badge keeps its dot; Sharing shows Retry. */ }
  }, options), [api, reads]);
  const badge = useCallback(() => loadShareStatus({ automatic: true }), [loadShareStatus]);

  const loadPane = useCallback(async (target: PaneId, options: PaneReadOptions = {}): Promise<void> => {
    const { workspace = latest.current.scope.workspace, configRequested } = options;
    if (!DATA_PANES.includes(target)) return;
    const key = paneKey(target, workspace);
    const force = options.force || reads.dirty.has(key);
    const current = latest.current.data;
    const present = target === 'roles' ? current.roles.has(workspace) : !!current[target as 'accounts' | 'sharing' | 'activity'];
    if (!force && present) return;
    const config = target === 'sharing' && !configRequested ? loadConfig(null, options) : undefined;
    await Promise.all([config, reads.request(key, async () => {
      setPaneErrors((errors) => ({ ...errors, [target]: undefined }));
      try {
        if (target === 'roles') {
          const value = await api.rolesShow(workspace) as RolesDocument;
          setData((previous) => ({ ...previous, roles: new Map(previous.roles).set(workspace, value) }));
        } else if (target === 'accounts') {
          const value = await api.accountsList();
          setData((previous) => ({ ...previous, accounts: value }));
        } else if (target === 'sharing') {
          const [listing] = await Promise.all([api.shareList(), loadShareStatus()]);
          // Settings closed before a status pass behind another could start: reload when next shown.
          if (reads.dirty.has('share-status')) reads.dirty.add('sharing');
          const result = shareStatus.current;
          if (!result) return;
          if ('error' in result) throw result.error;
          const sharing = { listing, status: result.value } as Omit<SharingData, 'config'>;
          setData((previous) => ({ ...previous, sharing }));
        } else {
          const value = await api.activityLog() as PaneData['activity'];
          setData((previous) => ({ ...previous, activity: value }));
        }
      } catch (error) {
        setPaneErrors((errors) => ({ ...errors, [target]: errorMessage(error) }));
      }
    }, options, force)]);
  }, [api, reads, loadConfig, loadShareStatus]);

  const reload = useCallback(async (target: PaneId = latest.current.pane) => {
    const { workspace } = latest.current.scope;
    await Promise.all([loadConfig(workspace), loadPane(target, { force: true, workspace, configRequested: workspace === null })]);
  }, [loadConfig, loadPane]);

  const moveTo = useCallback((target: SettingsTarget) => {
    tooltip.hide();
    const next: PaneId = target.key ? paneForKey(target.key) : isPane(target.pane) ? target.pane : 'general';
    const nextScope: Scope = { level: target.level ?? 'user', workspace: target.workspace ?? null };
    latest.current = { ...latest.current, pane: next, scope: nextScope };
    setPane(next);
    setScope(nextScope);
    const folder = nextScope.workspace;
    if (folder) setFolders((list) => (list.includes(folder) ? list : [...list, folder]));
    if (!target.preserveSearch) setQuery('');
    const key = target.key ?? (target.runtime && target.account ? `${target.runtime}:${target.account}` : null);
    setFlash(key ? { key, serial: ++flashSerial.current } : null);
    if (target.account === '') setAddAccount(true);
    return { next, workspace: nextScope.workspace };
  }, [tooltip]);

  const navigate = useCallback(async (target: SettingsTarget) => {
    const { next, workspace } = moveTo(target);
    await Promise.all([loadConfig(workspace), loadPane(next, { workspace, configRequested: workspace === null })]);
  }, [loadConfig, loadPane, moveTo]);

  // One history for the session, created once; its callbacks read the latest state.
  const callbacks = useRef({ navigate, reload: () => reload(), row: (_key: string, _message: string) => {} });
  callbacks.current.navigate = navigate;
  callbacks.current.reload = () => reload();
  callbacks.current.row = (key: string, message: string) => {
    const result = lastResult.current.get(key.startsWith('roles.') ? 'roles' : key);
    const stale = /changed elsewhere|changed since/i.test(message);
    setMessage(key, { tone: stale ? 'stale' : refusal(result)?.ask ? 'warn' : 'error', text: stale ? STALE : message });
    announce(stale ? STALE : message);
  };
  const history = useRef<SettingsHistory | null>(null);
  if (!history.current) {
    const remember = (key: string) => (result: unknown) => { lastResult.current.set(key, result); return result; };
    history.current = new SettingsHistory({
      set: (args: { key: string }) => api.configSet(args as Parameters<SettingsApi['configSet']>[0]).then(remember(args.key)),
      unset: (args: { key: string }) => api.configUnset(args as Parameters<SettingsApi['configUnset']>[0]).then(remember(args.key)),
      roles: (args: unknown) => api.rolesConfig(args as Parameters<SettingsApi['rolesConfig']>[0]).then(remember('roles')),
      navigate: (target: SettingsTarget) => callbacks.current.navigate(target),
      reload: () => callbacks.current.reload(),
      report: (key: string, message: string) => callbacks.current.row(key, message),
    });
  }

  const take = (key: string) => {
    if (busy.current) return false;
    busy.current = key;
    setBusyKey(key);
    return true;
  };
  const resumeRefresh = useRef(() => {});
  // Refreshes that arrived during the write waited for it: the reads already asked for, then the events.
  const release = () => { busy.current = null; setBusyKey(null); reads.resume(); resumeRefresh.current(); };

  const save: SettingsServices['save'] = useCallback(async (row, value, options = {}) => {
    const rowScope = options.scope ?? latest.current.scope;
    const document = options.document ?? latest.current.configs.get(rowScope.workspace);
    const view = rowState(row, rowScope.level, document);
    if (!view.editable || !take(row.key)) return;
    setMessage(row.key, null);
    const target = { pane: paneFor(row), workspace: rowScope.workspace, level: rowScope.level };
    // A field's draft keeps the revision it was typed against, so an outside edit
    // made meanwhile is refused as stale instead of silently overwritten.
    const revision = options.revision !== undefined ? options.revision : view.own?.revision ?? null;
    const args = { key: row.key, value, level: rowScope.level, workspace: rowScope.workspace, revision };
    try { await history.current?.commit(args, target, !!options.remove); }
    catch (error) {
      await reload();
      setMessage(row.key, { tone: 'error', text: errorMessage(error) });
    } finally { release(); }
  }, [reload, setMessage]);

  const mutate: SettingsServices['mutate'] = useCallback(async (key, target, work) => {
    if (!take(key)) return undefined;
    setMessage(key, null);
    try {
      const result = await work();
      const refused = refusal(result);
      if (refused) setMessage(key, { tone: refused.ask ? 'warn' : 'error', text: errorMessage(result) });
      return result;
    } catch (error) {
      setMessage(key, { tone: 'error', text: errorMessage(error) });
      return undefined;
    } finally {
      release();
      await loadPane(target, { force: true });
    }
  }, [loadPane, setMessage]);

  const roleWrite = useCallback(async (roleKey: string, edit: unknown) => {
    if (!take(roleKey)) return undefined;
    setMessage(roleKey, null);
    const current = latest.current.scope;
    try { return await history.current?.commitRole({ edit, ...current }, { pane: 'roles', key: roleKey, ...current }); }
    catch (error) { setMessage(roleKey, { tone: 'error', text: errorMessage(error) }); return undefined; }
    finally { release(); }
  }, [setMessage]);

  const edit = useCallback(async (action: 'undo' | 'redo') => {
    if (isTextField(document.activeElement)) { document.execCommand(action); return; }
    const steps = action === 'undo' ? history.current?.undoSteps : history.current?.redoSteps;
    const step = (steps as HistoryStep[] | undefined)?.at(-1);
    if (!step || !take('history')) return;
    try {
      const result = await history.current?.[action]();
      if (result && !refusal(result)) announce(`${action === 'undo' ? 'Undid' : 'Redid'} ${settingName(step.key)}`);
    } catch (error) { report(error); }
    finally { release(); }
  }, [announce, report]);

  const menuSubscription = useRef<Promise<Unlisten> | null>(null);
  const subscribeMenu = useCallback((handler: Parameters<SettingsApi['onPaneMenu']>[0]) => {
    menuSubscription.current = api.onPaneMenu(handler);
    return menuSubscription.current;
  }, [api]);
  const showMenu = useNativeMenu(api.showPaneMenu, subscribeMenu, report);

  const selectPane = useCallback((next: PaneId) => {
    tooltip.hide();
    latest.current = { ...latest.current, pane: next };
    setPane(next);
    loadPane(next).catch(report);
  }, [loadPane, report, tooltip]);

  const showHistory = useCallback((key: string) => {
    latest.current = { ...latest.current, pane: 'activity' };
    setPane('activity');
    document.body.dataset.historyKey = key;
    setQuery(key);
    loadPane('activity', { force: true }).catch(report);
  }, [loadPane, report]);

  const onStatus = useCallback((value: Envelope) => {
    setSnapshot(value);
    setFreshUsage(false);
  }, []);

  const refresh = (kinds: Set<RefreshKind | 'open'>) => {
    const { pane: current, scope: { workspace }, data: cached } = latest.current;
    const open = kinds.has('open'), config = kinds.has('config'), focus = kinds.has('focus'), timer = kinds.has('timer');
    const readConfig = open || config || focus || timer;
    // No event says a share.* setting changed, so a settings change and the timer reread Sharing too.
    const sharing = open || config || timer || kinds.has('share');
    const activity = open || kinds.has('activity');
    const accounts = open || config || focus;
    // Sharing shows the User file whatever workspace is selected.
    const userConfig = readConfig && workspace !== null && (current === 'sharing' || !!cached.sharing);
    if (readConfig) loadConfig(workspace, { automatic: true }).catch(report);
    if (userConfig) loadConfig(null, { automatic: true }).catch(report);
    // A pane off screen reloads when it is next selected. The native watcher
    // sends nothing while Settings is hidden, so an open marks them all. The
    // showing pane is read below; the scheduler marks it if that read can't start.
    const later = (target: PaneId, key: string = target) => { if (current !== target) reads.dirty.add(key); };
    if (sharing) later('sharing');
    if (activity) later('activity');
    if (accounts) later('accounts');
    // Workspace settings files aren't watched, so the timer marks cached Roles too.
    if (accounts || timer) {
      for (const folder of cached.roles.keys()) if (current !== 'roles' || folder !== workspace) reads.dirty.add(`roles:${folder ?? ''}`);
    }
    const showing = current === 'roles' ? readConfig : current === 'accounts' ? accounts
      : current === 'sharing' ? sharing : current === 'activity' && activity;
    // A pass on the showing pane found Settings hidden and was dropped. The app
    // had already sent the kind behind it, so it kept nothing for the restore:
    // any refresh, its focus included, reloads the pane.
    const dropped = DATA_PANES.includes(current) && reads.dirty.has(paneKey(current, workspace));
    if (showing || dropped) {
      loadPane(current, { force: true, automatic: true, workspace,
        configRequested: readConfig && (workspace === null || userConfig) }).catch(report);
    }
    if (sharing && current !== 'sharing') badge().catch(report);
  };
  const events = useRef({ edit, moveTo, refresh, showHistory, onStatus });
  events.current = { edit, moveTo, refresh, showHistory, onStatus };

  const readInstall = useCallback(async () => {
    try { setInstall(await api.getInstallStatus()); setInstallError(null); }
    catch (error) { setInstallError(errorMessage(error)); }
  }, [api]);
  const readUpdate = useCallback(async () => {
    try { setUpdate(await api.getUpdateStatus()); } catch (error) { report(error); }
  }, [api, report]);

  // Each subscription comes before its first read; one failure doesn't stop the others.
  useEffect(() => {
    let disposed = false;
    reads.start();
    const stops: Unlisten[] = [];
    const keep = (stop: Unlisten) => (disposed ? stop() : stops.push(stop));
    const step = (work: () => Promise<unknown>, failed: (error: unknown) => void = report) =>
      work().catch((error) => { if (!disposed) failed(error); });
    const listen = (subscription: Promise<Unlisten>, after?: () => Promise<unknown>, failed = report) =>
      step(async () => {
        keep(await subscription);
        if (after && !disposed) step(after, failed);
      }, failed);
    let ready = false;
    let receivedChecks = false;
    // A native open sends separate target and focus events. WebKit can pause
    // animation frames while the window is occluded, so collect them on a timer.
    let refreshTimer: number | undefined;
    const pending = new Set<RefreshKind | 'open'>();
    const schedule = (kinds: Iterable<RefreshKind | 'open'>) => {
      for (const kind of kinds) pending.add(kind);
      if (!ready || !pending.size || refreshTimer !== undefined || disposed || busy.current) return;
      refreshTimer = window.setTimeout(() => {
        refreshTimer = undefined;
        if (!disposed && !busy.current) { events.current.refresh(pending); pending.clear(); }
      }, SETTINGS_REFRESH_DELAY_MS);
    };
    resumeRefresh.current = () => schedule([]);
    let receivedTarget = false;
    let opened: number | undefined;
    const open = (target: SettingsTarget) => {
      if (disposed || (target.open_id !== undefined && opened === target.open_id)) return;
      opened = target.open_id;
      events.current.moveTo(target);
      schedule(['open']);
    };
    step(async () => {
      await Promise.all([
        menuSubscription.current?.catch(() => {}), // useNativeMenu reports its own failure.
        listen(api.onInstallStatus((value) => { setInstall(value); setInstallError(null); }), readInstall,
          (error) => setInstallError(errorMessage(error))),
        listen(api.onUpdateStatus(setUpdate), readUpdate),
        listen(api.onAccountChecksStatus((value) => { receivedChecks = true; if (!disposed) setChecks(value); }), async () => {
          const value = await api.getAccountChecks();
          if (!disposed && !receivedChecks) setChecks(value);
        }),
        listen(api.onSettingsEdit(({ action }) => { events.current.edit(action); })),
        listen(api.onSettingsTarget((target) => { receivedTarget = true; open(target); })),
        listen(api.onSettingsRefresh(({ kinds }) => schedule(kinds))),
        listen(api.onSettingsHistory(({ key }) => events.current.showHistory(key))),
        listen(api.onUsageUpdated((value) => {
          const animate = value.generation > freshGeneration.current && document.visibilityState !== 'hidden';
          freshGeneration.current = Math.max(freshGeneration.current, value.generation);
          setSnapshot(value);
          setFreshUsage(animate);
        })),
        listen(api.onSettingsStatus((value) => events.current.onStatus(value))),
      ]);
      if (disposed) return;
      step(async () => { const value = await api.getSnapshot(); if (!disposed) { setSnapshot(value); setFreshUsage(false); } });
      try {
        if (await api.settingsVisible() && !disposed && !receivedTarget) {
          const target = await api.getSettingsTarget();
          if (!receivedTarget) open(target ?? { pane: 'general' });
        }
      } finally {
        ready = true;
        schedule([]);
      }
    });
    step(async () => { const value = await api.getOpenAtLogin(); if (!disposed) setLogin({ value, readable: true, busy: false }); },
      () => setLogin({ value: null, readable: false, busy: false }));
    step(async () => { const rows = await api.testBuildPaths(); if (!disposed) setTestRows(testBuildRows(rows)); }, () => setTestRows([]));
    return () => {
      disposed = true;
      window.clearTimeout(refreshTimer);
      reads.dispose();
      resumeRefresh.current = () => {};
      stops.splice(0).forEach((stop) => stop());
    };
  }, [api, reads, readInstall, readUpdate, report]);

  // ⌘F focuses Search; ⌘Z / ⇧⌘Z undo outside text fields (the native Edit menu sends settings-edit too).
  useEffect(() => {
    const keyDown = (event: KeyboardEvent) => {
      if (!event.metaKey || event.altKey || event.ctrlKey) return;
      const key = event.key.toLowerCase();
      if (key === 'f') { event.preventDefault(); search.current?.focus(); search.current?.select(); }
      if (key === 'z' && !isTextField(document.activeElement)) { event.preventDefault(); events.current.edit(event.shiftKey ? 'redo' : 'undo'); }
    };
    document.addEventListener('keydown', keyDown);
    return () => document.removeEventListener('keydown', keyDown);
  }, []);

  // The first search text loads the panes not read yet, once, for the sidebar counts.
  useEffect(() => {
    if (!query.trim() || preloaded.current) return;
    preloaded.current = true;
    for (const target of DATA_PANES) loadPane(target).catch(report);
  }, [query, loadPane, report]);

  const config = configs.get(scope.workspace) ?? null;
  const userConfig = configs.get(null);
  const sharing = useMemo(() => data.sharing && userConfig ? { ...data.sharing, config: userConfig } : null, [data.sharing, userConfig]);
  const usage = snapshot?.usage ?? null;
  const accounts = useMemo(() => (data.accounts ? accountModels(data.accounts, usage, now() / 1000, checks) : []), [data.accounts, usage, checks]);
  const searching = query.trim() !== '';
  const counts = useMemo(() => {
    if (!searching) return null;
    const sources = { config, roles: data.roles.get(scope.workspace) ?? null, accounts, sharing,
      activity: data.activity?.entries ?? [], testRows };
    return Object.fromEntries(PANE_IDS.map((id) => [id, paneCount(id, query, sources)])) as Record<PaneId, number>;
  }, [searching, query, config, data, sharing, accounts, testRows, scope.workspace]);

  const install_ = installState(snapshot ?? {}) as { problem: { message: string; action: string } | null };
  const generalAttention = !!install_.problem || !!install?.setup_unknown;
  const items: NavItem[] = PANE_IDS.map((id) => ({
    id, label: PANE_TITLES[id], divider: id === 'claude',
    attention: id === 'general' ? generalAttention : id === 'sharing' ? attention : false,
    count: counts ? counts[id] : null,
  }));

  const consume = useCallback((serial: number) => setFlash((current) => (current?.serial === serial ? null : current)), []);
  const flashValue = useMemo(() => ({ target: flash, consume }), [flash, consume]);

  const services: SettingsServices = {
    api, scope, query, busyKey, messages, setMessage, save, mutate, roleWrite, navigate,
    showMenu: (event: MouseEvent | null, actions) => showMenu(event, actions), report,
    reload: (target) => reload(target), openAddAccount: () => setAddAccount(true), reduced, now, timeZone,
  };

  const installSheet = install?.dialog ? { id: 'installer', node: <InstallerSheet status={install} api={api} reread={readInstall} /> } : null;
  const updateSheet = update?.dialog ? { id: 'updater', node: <UpdaterSheet status={update} api={api} reread={readUpdate} /> } : null;
  const addSheet = addAccount ? { id: 'add-account', node: <AddAccountSheet api={api} onClose={(added) => {
    setAddAccount(false);
    if (added) loadPane('accounts', { force: true }).catch(report);
  }} /> } : null;
  const sheetOpen = !!(installSheet || updateSheet || addSheet);

  let body;
  if (pane === 'roles') body = <RolesPane document={data.roles.get(scope.workspace) ?? null} error={paneErrors.roles} config={config} />;
  else if (pane === 'accounts') body = <AccountsPane models={accounts} loaded={!!data.accounts} error={paneErrors.accounts} animate={freshUsage} />;
  else if (pane === 'sharing') body = <SharingPane data={sharing} error={paneErrors.sharing ?? userConfigError ?? undefined} />;
  else if (pane === 'activity') body = <ActivityPane data={data.activity ?? null} error={paneErrors.activity} config={config} />;
  else if (pane === 'general') {
    body = <GeneralPane config={config} configError={configError} snapshot={snapshot} install={install} installError={installError}
      readInstall={readInstall} login={login} setLogin={setLogin} testRows={testRows} />;
  } else body = <ProviderPane provider={pane} config={config} configError={configError} />;

  return (
    <SettingsContext.Provider value={services}>
      <FlashContext.Provider value={flashValue}>
        <aside className="sidebar" inert={sheetOpen}>
          <div className="window-drag" data-tauri-drag-region />
          <SearchField value={query} onChange={setQuery} inputRef={search} />
          <SidebarNav items={items} selected={pane} onSelect={(id) => selectPane(id as PaneId)} />
        </aside>
        <div className="main" inert={sheetOpen}>
          <header className="toolbar" data-tauri-drag-region aria-label="Settings toolbar">
            <h1 id="pane-title" className="toolbar-title" tabIndex={-1} data-tauri-drag-region>{PANE_TITLES[pane]}</h1>
            {SCOPE_PANES.includes(pane) && (
              <ScopePopup scope={scope} folders={folders}
                onSelect={(next) => { navigate({ pane, level: next.level, workspace: next.workspace, preserveSearch: true }).catch(report); }}
                onChoose={() => {
                  api.chooseWorkspace().then((folder) => {
                    if (folder) return navigate({ pane, level: 'workspace', workspace: folder, preserveSearch: true });
                    return undefined;
                  }).catch(report);
                }} />
            )}
          </header>
          <main id="pane-content" className={`pane-content pane-${pane}`} aria-label={`${PANE_TITLES[pane]} settings`}>{body}</main>
        </div>
        <SheetHost sheets={[installSheet, updateSheet, addSheet]} fallbackFocus={() => document.getElementById('pane-title')} />
        <div id="status" className="visually-hidden" role="status">{status}</div>
      </FlashContext.Provider>
    </SettingsContext.Provider>
  );
}
