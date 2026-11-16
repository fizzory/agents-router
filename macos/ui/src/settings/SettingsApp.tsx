// The Settings window: sidebar, toolbar with the scope popup, the selected
// pane only, and one sheet host. Every write goes through one busy guard;
// config writes and role edits join the serialized session history.
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type MouseEvent } from 'react';
import { SettingsHistory } from '../../logic/settings-history.js';
import { isTextField } from '../../logic/settings.js';
import { sharingModel as sharingModelJs } from '../../logic/panes.js';
import {
  AnnounceProvider, FlashContext, ScopePopup, SearchField, SheetHost, SidebarNav, TooltipProvider, useAnnounce, useNativeMenu,
  useReducedMotion, useTooltip, type FlashTarget, type NavItem, type RowMessage, type Scope,
} from '../shared';
import { installState } from '../../logic/settings.js';
import type { ConfigShow, Envelope, InstallStatus, SettingsApi, SettingsTarget, UpdateStatus, Unlisten } from './api';
import { SettingsContext, refusal, type SettingsServices } from './context';
import {
  accountModels, errorMessage, isPane, PANE_IDS, PANE_TITLES, paneCount, paneFor, paneForKey, PROVIDER_PANES, rowState, settingName,
  testBuildRows, type ActivityEntry, type PaneId, type RolesDocument, type SharingData, type TestRow,
} from './model';
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

interface PaneData { roles: Map<string | null, RolesDocument>; accounts?: unknown; sharing?: SharingData; activity?: { entries?: ActivityEntry[]; more?: boolean } }
type HistoryStep = { key: string };
const DATA_PANES: PaneId[] = ['roles', 'accounts', 'sharing', 'activity'];
const STALE = 'Changed elsewhere. Showing the current value.';
const SCOPE_PANES: PaneId[] = ['general', 'roles', ...PROVIDER_PANES];

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
  const configGeneration = useRef(new Map<string | null, number>());
  const loading = useRef(new Map<string, boolean>());
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

  const loadConfig = useCallback(async (workspace: string | null) => {
    const generation = (configGeneration.current.get(workspace) ?? 0) + 1;
    configGeneration.current.set(workspace, generation);
    try {
      const value = await api.configShow(workspace);
      if (configGeneration.current.get(workspace) !== generation) return;
      setConfigs((current) => new Map(current).set(workspace, value));
      if (latest.current.scope.workspace === workspace) { setConfigError(null); setStatus(''); }
    } catch (error) {
      if (configGeneration.current.get(workspace) !== generation || latest.current.scope.workspace !== workspace) return;
      setConfigError(errorMessage(error));
      report(error);
    }
  }, [api, report]);

  const badge = useCallback(async () => {
    try {
      const value = await api.shareStatus() as { attention?: unknown[] };
      setAttention(!!value?.attention?.length);
    } catch { /* Sharing shows its own Retry. */ }
  }, [api]);

  const loadPane = useCallback(async (target: PaneId, force = false, workspace = latest.current.scope.workspace): Promise<void> => {
    if (!DATA_PANES.includes(target)) return;
    const key = target === 'roles' ? `roles:${workspace}` : target;
    const current = latest.current.data;
    const present = target === 'roles' ? current.roles.has(workspace) : !!current[target as 'accounts' | 'sharing' | 'activity'];
    if (loading.current.has(key)) {
      // A forced reload during a read runs again once that read lands.
      if (force) loading.current.set(key, true);
      return;
    }
    if (!force && present) return;
    loading.current.set(key, false);
    setPaneErrors((errors) => ({ ...errors, [target]: undefined }));
    try {
      if (target === 'roles') {
        const value = await api.rolesShow(workspace) as RolesDocument;
        setData((previous) => ({ ...previous, roles: new Map(previous.roles).set(workspace, value) }));
      } else if (target === 'accounts') {
        const value = await api.accountsList();
        setData((previous) => ({ ...previous, accounts: value }));
      } else if (target === 'sharing') {
        const [listing, statusValue, config] = await Promise.all([api.shareList(), api.shareStatus(), api.configShow(null)]);
        const sharing = { listing, status: statusValue, config } as SharingData;
        setData((previous) => ({ ...previous, sharing }));
        setConfigs((configsNow) => new Map(configsNow).set(null, config));
        setAttention(!!(sharingModelJs(listing, statusValue) as { waiting: unknown[] }).waiting.length);
      } else {
        const value = await api.activityLog() as PaneData['activity'];
        setData((previous) => ({ ...previous, activity: value }));
      }
    } catch (error) {
      setPaneErrors((errors) => ({ ...errors, [target]: errorMessage(error) }));
    } finally {
      const again = loading.current.get(key);
      loading.current.delete(key);
      if (again) await loadPane(target, true, workspace);
    }
  }, [api]);

  const reload = useCallback(async (target: PaneId = latest.current.pane) => {
    await Promise.all([loadConfig(latest.current.scope.workspace), loadPane(target, true)]);
  }, [loadConfig, loadPane]);

  const navigate = useCallback(async (target: SettingsTarget) => {
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
    await Promise.all([loadConfig(nextScope.workspace), loadPane(next, false, nextScope.workspace)]);
  }, [loadConfig, loadPane, tooltip]);

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
  const release = () => { busy.current = null; setBusyKey(null); };

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
      await loadPane(target, true);
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

  const showMenu = useNativeMenu(api.showPaneMenu, api.onPaneMenu, report);

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
    loadPane('activity', true).catch(report);
  }, [loadPane, report]);

  const onStatus = useCallback((value: Envelope) => {
    setSnapshot(value);
    setFreshUsage(false);
    if (busy.current) return;
    const current = latest.current;
    loadConfig(current.scope.workspace).catch(report);
    loadPane(current.pane, true).catch(report);
    if (current.pane !== 'sharing') badge();
  }, [badge, loadConfig, loadPane, report]);

  const events = useRef({ edit, navigate, showHistory, onStatus });
  events.current = { edit, navigate, showHistory, onStatus };

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
    const stops: Unlisten[] = [];
    const keep = (stop: Unlisten) => (disposed ? stop() : stops.push(stop));
    const step = (work: () => Promise<unknown>, failed: (error: unknown) => void = report) =>
      work().catch((error) => { if (!disposed) failed(error); });
    step(async () => { keep(await api.onInstallStatus((value) => { setInstall(value); setInstallError(null); })); await readInstall(); },
      (error) => setInstallError(errorMessage(error)));
    step(async () => { keep(await api.onUpdateStatus(setUpdate)); await readUpdate(); });
    step(async () => {
      keep(await api.onSettingsEdit(({ action }) => { events.current.edit(action); }));
      keep(await api.onSettingsTarget((target) => { events.current.navigate(target).catch(report); }));
      keep(await api.onSettingsHistory(({ key }) => events.current.showHistory(key)));
      keep(await api.onUsageUpdated((value) => {
        // Only a newer generation on a showing window animates; anything else snaps.
        const animate = value.generation > freshGeneration.current && document.visibilityState !== 'hidden';
        freshGeneration.current = Math.max(freshGeneration.current, value.generation);
        setSnapshot(value);
        setFreshUsage(animate);
      }));
      keep(await api.onSettingsStatus((value) => events.current.onStatus(value)));
      step(async () => { const value = await api.getSnapshot(); if (!disposed) { setSnapshot(value); setFreshUsage(false); } });
      const target = await api.getSettingsTarget();
      if (disposed) return;
      await events.current.navigate(target ?? { pane: 'general' });
      if (!disposed && latest.current.pane !== 'sharing') await badge();
    });
    step(async () => { const value = await api.getOpenAtLogin(); if (!disposed) setLogin({ value, readable: true, busy: false }); },
      () => setLogin({ value: null, readable: false, busy: false }));
    step(async () => { const rows = await api.testBuildPaths(); if (!disposed) setTestRows(testBuildRows(rows)); }, () => setTestRows([]));
    return () => { disposed = true; stops.splice(0).forEach((stop) => stop()); };
  }, [api, badge, readInstall, readUpdate, report]);

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
  const usage = snapshot?.usage ?? null;
  const accounts = useMemo(() => (data.accounts ? accountModels(data.accounts, usage, now() / 1000) : []), [data.accounts, usage]);
  const searching = query.trim() !== '';
  const counts = useMemo(() => {
    if (!searching) return null;
    const sources = { config, roles: data.roles.get(scope.workspace) ?? null, accounts, sharing: data.sharing ?? null,
      activity: data.activity?.entries ?? [], testRows };
    return Object.fromEntries(PANE_IDS.map((id) => [id, paneCount(id, query, sources)])) as Record<PaneId, number>;
  }, [searching, query, config, data, accounts, testRows, scope.workspace]);

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
    if (added) loadPane('accounts', true).catch(report);
  }} /> } : null;
  const sheetOpen = !!(installSheet || updateSheet || addSheet);

  let body;
  if (pane === 'roles') body = <RolesPane document={data.roles.get(scope.workspace) ?? null} error={paneErrors.roles} config={config} />;
  else if (pane === 'accounts') body = <AccountsPane models={accounts} loaded={!!data.accounts} error={paneErrors.accounts} animate={freshUsage} />;
  else if (pane === 'sharing') body = <SharingPane data={data.sharing ?? null} error={paneErrors.sharing} />;
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
