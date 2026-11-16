// The Settings window's slice of tauri.js (the only window.__TAURI__ boundary).
// main.tsx passes the real adapter; tests pass a fake with the same shape.
import type { Envelope, Unlisten } from '../popover/api';
import type { PaneMenuEvent } from '../shared';

export type { Envelope, Unlisten };
export type Level = 'user' | 'workspace' | 'local';
type Listen<T> = (handler: (payload: T) => void) => Promise<Unlisten>;

export interface LevelValue { present: boolean; value: unknown; file?: string | null; line?: number | null; revision?: string | null }
export interface ConfigRow {
  key: string;
  value: unknown;
  source: string;
  file?: string | null;
  line?: number | null;
  description: string;
  applies?: string;
  kind: string;
  choices?: unknown[] | null;
  minimum?: number | null;
  editable: boolean;
  accepted_levels?: Level[];
  still_limited_by?: string[] | null;
  levels: Partial<Record<Level, LevelValue>>;
}
export interface ConfigShow { workspace?: string | null; files: { level: string; file: string; exists: boolean }[]; error?: unknown; settings: ConfigRow[] }
export interface SettingsTarget { pane?: string; runtime?: string | null; account?: string | null; key?: string; level?: Level; workspace?: string | null; preserveSearch?: boolean }
export interface InstallStatus {
  dialog: string | null;
  message?: string | null;
  version?: string | null;
  foreign?: { target: string; version: string; source: string } | null;
  bundle_version?: string | null;
  busy?: boolean;
  setup_unknown?: boolean;
  can_restore?: boolean;
}
export interface UpdateDialog { title: string; detail: string; buttons: [string, string][]; default: string; busy: boolean }
export interface UpdateStatus { dialog: UpdateDialog | null; received?: number | null; total?: number | null }
export interface TestBuildPath { key: string; path: string; display?: string; source?: string; origin?: string }

type Call<A extends unknown[] = [], R = unknown> = (...args: A) => Promise<R>;

export interface SettingsApi {
  getSnapshot: Call<[], Envelope>;
  checkForUpdates: Call;
  configShow: Call<[workspace: string | null], ConfigShow>;
  configSet: Call<[args: { key: string; value: string; level: Level; workspace: string | null; revision?: string | null; userSaid?: string | null }]>;
  configUnset: Call<[args: { key: string; level: Level; workspace: string | null; revision?: string | null; userSaid?: string | null }]>;
  configRevert: Call<[args: { entry: string; workspace: string | null }]>;
  rolesShow: Call<[workspace: string | null]>;
  rolesConfig: Call<[args: { edit: unknown; level: Level; workspace: string | null }]>;
  accountsList: Call;
  accountsCheck: Call<[runtime: string, account: string]>;
  accountsAddCheck: Call<[runtime: string, account: string]>;
  accountsToggle: Call<[runtime: string, account: string, enabled: boolean]>;
  accountsLogin: Call<[runtime: string, account: string]>;
  accountsAdd: Call<[runtime: string, account: string]>;
  shareList: Call;
  shareStatus: Call;
  shareKind: Call<[kind: string, enabled: boolean]>;
  sharePick: Call<[item: string, label: string, included: boolean]>;
  activityLog: Call;
  openSettingsFile: Call<[file: string, line?: number]>;
  chooseWorkspace: Call<[], string | null>;
  getOpenAtLogin: Call<[], boolean>;
  setOpenAtLogin: Call<[enabled: boolean], boolean>;
  getSettingsTarget: Call<[], SettingsTarget>;
  showSettingMenu: Call<[key: string]>;
  showPaneMenu: Call<[token: string, actions: string[]]>;
  activityUndoSync: Call<[entry: string]>;
  copyText: Call<[text: string]>;
  setupRoles: Call;
  getInstallStatus: Call<[], InstallStatus>;
  installAction: Call<[action: string]>;
  getUpdateStatus: Call<[], UpdateStatus>;
  updateAction: Call<[action: string]>;
  testBuildPaths: Call<[], TestBuildPath[] | null>;
  onUsageUpdated: Listen<Envelope>;
  onSettingsStatus: Listen<Envelope>;
  onSettingsTarget: Listen<SettingsTarget>;
  onSettingsHistory: Listen<{ key: string }>;
  onSettingsEdit: Listen<{ action: 'undo' | 'redo' }>;
  onPaneMenu: Listen<PaneMenuEvent>;
  onInstallStatus: Listen<InstallStatus>;
  onUpdateStatus: Listen<UpdateStatus>;
}
