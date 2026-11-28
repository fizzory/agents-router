// A fake Settings adapter: every command answers from tests/fixtures/app, every
// event can be emitted, and unlistens are recorded. Plus render helpers.
import { act, render } from '@testing-library/react';
import { vi } from 'vitest';
import accountsList from '../../../../tests/fixtures/app/accounts-list-locked.json';
import activity from '../../../../tests/fixtures/app/activity-log.json';
import configSet from '../../../../tests/fixtures/app/config-set.json';
import configShow from '../../../../tests/fixtures/app/config-show.json';
import configUnset from '../../../../tests/fixtures/app/config-unset-absent.json';
import roles from '../../../../tests/fixtures/app/roles-show-panes.json';
import rolesConfig from '../../../../tests/fixtures/app/roles-config.json';
import shareList from '../../../../tests/fixtures/app/share-list-kinds.json';
import shareStatus from '../../../../tests/fixtures/app/share-status-waiting.json';
import usage from '../../../../tests/fixtures/app/usage-popover.json';
import type { Envelope, InstallStatus, SettingsApi, SettingsTarget, UpdateStatus } from '../settings/api';
import { SETTINGS_REFRESH_DELAY_MS, SettingsApp } from '../settings/SettingsApp';
import { resetDisclosures } from '../shared';

export const fixtures = { accountsList, activity, configSet, configShow, configUnset, roles, rolesConfig, shareList, shareStatus, usage };
export const NOW = Date.parse('2027-01-15T12:00:00Z');
const copy = <T,>(value: T): T => structuredClone(value);
export const envelope = (generation = 1): Envelope => ({ usage: copy(usage), error: null, reading: false, generation, update_version: null });

type Handler = (payload: never) => void;
const EVENTS = ['onAccountChecksStatus', 'onUsageUpdated', 'onSettingsStatus', 'onSettingsRefresh', 'onSettingsTarget', 'onSettingsHistory', 'onSettingsEdit', 'onPaneMenu', 'onInstallStatus', 'onUpdateStatus'] as const;
type EventName = typeof EVENTS[number];

export interface FakeOptions {
  target?: SettingsTarget;
  visible?: boolean;
  install?: InstallStatus;
  update?: UpdateStatus;
  config?: unknown;
  testPaths?: unknown;
}

export function fakeSettingsApi(options: FakeOptions = {}) {
  const handlers = new Map<EventName, Handler[]>();
  const unlistened: string[] = [];
  const done = () => Promise.resolve(undefined);
  const listen = (name: EventName) => vi.fn((handler: Handler) => {
    handlers.set(name, [...(handlers.get(name) ?? []), handler]);
    return Promise.resolve(() => { unlistened.push(name); handlers.set(name, (handlers.get(name) ?? []).filter((item) => item !== handler)); });
  });
  const api = {
    getAccountChecks: vi.fn(() => Promise.resolve([])),
    getSnapshot: vi.fn(() => Promise.resolve(envelope())),
    checkForUpdates: vi.fn(done),
    configShow: vi.fn((_workspace: string | null) => Promise.resolve(copy(options.config ?? configShow))),
    configSet: vi.fn((_args: unknown) => Promise.resolve(copy(configSet) as unknown)),
    configUnset: vi.fn((_args: unknown) => Promise.resolve(copy(configUnset) as unknown)),
    configRevert: vi.fn(done),
    rolesShow: vi.fn(() => Promise.resolve(copy(roles))),
    rolesConfig: vi.fn((_args: unknown) => Promise.resolve(copy(rolesConfig) as unknown)),
    accountsList: vi.fn(() => Promise.resolve(copy(accountsList))),
    accountsCheck: vi.fn(done),
    accountsAddCheck: vi.fn((_runtime: string, account: string) => Promise.resolve({ ok: true, account, runtime: 'claude' } as unknown)),
    accountsToggle: vi.fn((_runtime: string, account: string, enabled: boolean) => Promise.resolve({ ok: true, account, enabled } as unknown)),
    accountsLogin: vi.fn(() => Promise.resolve({ status: 'opened', copy_command: 'fake login command' })),
    accountsAdd: vi.fn((_runtime: string, _account: string) => Promise.resolve(undefined as unknown)),
    shareList: vi.fn(() => Promise.resolve(copy(shareList) as unknown)),
    shareStatus: vi.fn(() => Promise.resolve(copy(shareStatus) as unknown)),
    shareKind: vi.fn((_kind: string, _enabled: boolean) => Promise.resolve({ kind: 'mcp', enabled: true, unchanged: true } as unknown)),
    sharePick: vi.fn(done),
    activityLog: vi.fn(() => Promise.resolve(copy(activity) as unknown)),
    openSettingsFile: vi.fn(done),
    chooseWorkspace: vi.fn(() => Promise.resolve(null as string | null)),
    getOpenAtLogin: vi.fn(() => Promise.resolve(false)),
    setOpenAtLogin: vi.fn((enabled: boolean) => Promise.resolve(enabled)),
    settingsVisible: vi.fn(() => Promise.resolve(options.visible ?? true)),
    getSettingsTarget: vi.fn(() => Promise.resolve(options.target ?? { pane: 'general' })),
    showSettingMenu: vi.fn(done),
    showPaneMenu: vi.fn((_token: string, _actions: string[]) => done()),
    activityUndoSync: vi.fn(done),
    copyText: vi.fn((_text: string) => done()),
    setupRoles: vi.fn(done),
    getInstallStatus: vi.fn(() => Promise.resolve(options.install ?? { dialog: null } as InstallStatus)),
    installAction: vi.fn((_action: string) => done()),
    getUpdateStatus: vi.fn(() => Promise.resolve(options.update ?? { dialog: null } as UpdateStatus)),
    updateAction: vi.fn((_action: string) => done()),
    testBuildPaths: vi.fn(() => Promise.resolve((options.testPaths ?? null) as never)),
    ...Object.fromEntries(EVENTS.map((name) => [name, listen(name)])),
  } as unknown as SettingsApi & Record<EventName, ReturnType<typeof listen>>;
  const dispatch = (name: EventName, payload: unknown) => {
    for (const handler of handlers.get(name) ?? []) handler(payload as never);
  };
  const emit = async (name: EventName, payload: unknown) => {
    if (name === 'onSettingsTarget' || name === 'onSettingsRefresh') await throughRefresh(() => dispatch(name, payload));
    else await act(async () => dispatch(name, payload));
    await settle();
  };
  return { api: api as SettingsApi & { [K in keyof SettingsApi]: ReturnType<typeof vi.fn> }, dispatch, emit, unlistened, handlers };
}

/**
 * Runs `work`, which makes Settings collect a refresh batch, and lets the batch
 * delay pass on a fake clock: no test waits real time for it. The reads the
 * batch starts are fake adapter calls that resolve in microtasks, so `settle`
 * finishes them; a test that holds one open awaits its own promise.
 */
export async function throughRefresh(work: () => void) {
  // A test that already runs on fake timers keeps them.
  const own = !vi.isFakeTimers();
  if (own) vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
  try {
    await act(async () => work());
    await settle();
    await act(async () => { await vi.advanceTimersByTimeAsync(SETTINGS_REFRESH_DELAY_MS); });
  } finally {
    if (own) vi.useRealTimers();
  }
  await settle();
}

export async function settle(rounds = 6) {
  for (let index = 0; index < rounds; index += 1) await act(async () => { await Promise.resolve(); });
}

/** `prepare` changes the fake adapter before the window mounts. */
export async function renderSettings(options: FakeOptions = {}, prepare?: (fake: ReturnType<typeof fakeSettingsApi>) => void) {
  resetDisclosures();
  const fake = fakeSettingsApi(options);
  prepare?.(fake);
  let view!: ReturnType<typeof render>;
  await throughRefresh(() => { view = render(<SettingsApp api={fake.api} now={() => NOW} timeZone="UTC" />); });
  return { ...fake, ...view };
}

export const nav = (pane: string) => document.querySelector<HTMLButtonElement>(`nav button[data-pane="${pane}"]`);
export const settingRow = (key: string) => document.querySelector<HTMLElement>(`[data-setting-key="${key}"]`);
export const paneKeys = () => [...document.querySelectorAll<HTMLElement>('#pane-content [data-setting-key]')].map((node) => node.dataset.settingKey);
