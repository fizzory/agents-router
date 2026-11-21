// Typed views over the pure logic modules, and search per pane.
import { accountModels as accountModelsJs, filterActivity as filterActivityJs, settingName as settingNameJs, SHARE_LABELS, sharingModel as sharingModelJs,
  valueLabel as valueLabelJs } from '../../logic/panes.js';
import { displayValue as displayValueJs, errorMessage as errorMessageJs, labelFor as labelForJs, LEVELS, matches as matchesJs,
  PANES, paneFor as paneForJs, paneForKey as paneForKeyJs, PROVIDERS, rowState as rowStateJs } from '../../logic/settings.js';
import { testBuildMatches as testBuildMatchesJs, testBuildRows as testBuildRowsJs } from '../../logic/test-build.js';
import type { AccountCheck, RowModel } from '../shared';
import type { ConfigRow, ConfigShow, Level, LevelValue, TestBuildPath } from './api';

export type PaneId = 'general' | 'roles' | 'accounts' | 'sharing' | 'activity' | 'claude' | 'codex' | 'antigravity' | 'kiro';
export const PANE_IDS: PaneId[] = ['general', 'roles', 'accounts', 'sharing', 'activity', 'claude', 'codex', 'antigravity', 'kiro'];
export const PROVIDER_PANES: PaneId[] = ['claude', 'codex', 'antigravity', 'kiro'];
export const PANE_TITLES = PANES as Record<PaneId, string>;
export const PROVIDER_NAMES = PROVIDERS as Record<string, string>;
export const LEVEL_NAMES = LEVELS as Record<Level, string>;
export const SHARE_NAMES = SHARE_LABELS as Record<string, string>;
export const isPane = (value: unknown): value is PaneId => typeof value === 'string' && (PANE_IDS as string[]).includes(value);

export interface RowState {
  value: unknown;
  own?: LevelValue;
  level: Level;
  editable: boolean;
  reset: boolean;
  chip: string | null;
  chipLevel: Level | null;
  chipTooltip: string;
  label: string;
  tooltip: string;
}
export const rowState = (row: ConfigRow, level: Level, document: ConfigShow | null | undefined) => rowStateJs(row, level, document ?? {}) as RowState;
export const labelFor = (key: string) => labelForJs(key) as string;
export const displayValue = (value: unknown) => displayValueJs(value) as string;
export const errorMessage = (value: unknown) => errorMessageJs(value) as string;
export const paneFor = (row: ConfigRow) => paneForJs(row) as PaneId;
export const paneForKey = (key: string) => paneForKeyJs(key) as PaneId;
export const matches = (row: ConfigRow, query: string) => matchesJs(row, query) as boolean;
export const lower = (text: string) => text.charAt(0).toLowerCase() + text.slice(1);

/** "Claude max jobs", "Max depth", "builder role": what undo, redo and Activity say. */
export const settingName = (key: string) => settingNameJs(key) as string;
/** "Not shared" for share.instructions off, "Every CLI" for New items on; null when the raw value reads fine. */
export const valueLabel = (key: string, value: unknown) => valueLabelJs(key, value) as string | null;

export const has = (query: string, ...texts: (string | null | undefined)[]) =>
  texts.filter(Boolean).join(' ').toLowerCase().includes(query.trim().toLowerCase());

export interface TestRow { key: string; label: string; path: string; value: string; full: string; chip: string | null; copyLabel: string }
export const testBuildRows = (rows: TestBuildPath[] | null) => testBuildRowsJs(rows) as TestRow[];
export const testBuildMatches = (rows: TestRow[], query: string) => testBuildMatchesJs(rows, query) as boolean;

export interface Role {
  name: string; access: string; enabled: boolean; use_when?: string | null; level?: string;
  providers: { provider: string; entry: string; model?: string | null; effort?: string | null; usable: boolean; reason?: string | null; choices?: unknown }[];
  sources?: Record<string, { level: string; file?: string; line?: number }>;
}
export interface RolesDocument { roles: Role[]; error?: unknown; needs_user_words?: unknown }
export const roleMatches = (role: Role, query: string) => has(query, role.name, role.use_when, ...role.providers.map((provider) => provider.entry));

export interface AccountModel extends RowModel {
  account: { name: string; enabled: boolean; can_toggle?: boolean; toggle_reason?: string };
  reason: string | null;
  toggle: { kind: 'switch'; label: string; enabled: boolean } | { kind: 'badge'; text: string; reason: string };
}
export const accountModels = (list: unknown, usage: unknown, now: number, checks: AccountCheck[] = []) => accountModelsJs(list, usage, now, checks) as AccountModel[];
export const accountMatches = (model: AccountModel, query: string) => has(query, model.provider, model.account.name, model.tooltip);

export interface ShareCellModel { label: string; included: boolean; editable: boolean; reason: string; command: string }
export interface ShareItemModel { id: string; kind: string; name: string; cells: ShareCellModel[] }
export interface Waiting { item?: string; message: string; command: string }
export interface SharingData { listing: { enabled?: boolean; folders?: { label: string; cli: string; path: string }[]; items?: unknown[] }; status: unknown; config: ConfigShow }
export interface SharingModel { enabled: boolean; state: string | null; command: string; waiting: Waiting[]; items: ShareItemModel[] }
export const sharingModel = (data: SharingData) => sharingModelJs(data.listing, data.status) as SharingModel;
export const shareRowMatches = (row: ConfigRow, query: string) => has(query, SHARE_NAMES[row.key], row.key, row.description);
export const itemMatches = (item: ShareItemModel, query: string) => has(query, item.name, item.kind, item.id);

export interface ActivityEntry { id: string; time: string; type: string; key?: string; level?: string; workspace?: string; family?: string; file?: string; line?: number;
  changes?: { ok?: boolean; item?: string; backup?: unknown }[] }
export interface ActivityModel { entry: ActivityEntry; time: string; day: string; label: string; change: string; parts: [string, string] | null; level: string; marker: string; tooltip: string }
export const filterActivity = (entries: ActivityEntry[], query: string, settings: ConfigRow[], options: { now?: number; timeZone?: string } = {}) =>
  filterActivityJs(entries, query, settings, options) as ActivityModel[];

/** How many results each pane has for a search, for the sidebar counts. */
export interface PaneSources {
  config: ConfigShow | null;
  roles: RolesDocument | null;
  accounts: AccountModel[];
  sharing: SharingData | null;
  activity: ActivityEntry[];
  testRows: TestRow[];
}
export function paneCount(pane: PaneId, query: string, data: PaneSources): number {
  const settings = data.config?.settings ?? [];
  if (pane === 'general') {
    return settings.filter((row) => paneFor(row) === 'general' && matches(row, query)).length
      + data.testRows.filter((row) => testBuildMatches([row], query)).length;
  }
  if ((PROVIDER_PANES as string[]).includes(pane)) return settings.filter((row) => paneFor(row) === pane && matches(row, query)).length;
  if (pane === 'roles') return (data.roles?.roles ?? []).filter((role) => roleMatches(role, query)).length;
  if (pane === 'accounts') return data.accounts.filter((model) => accountMatches(model, query)).length;
  if (pane === 'sharing') {
    if (!data.sharing) return 0;
    const model = sharingModel(data.sharing);
    return data.sharing.config.settings.filter((row) => paneFor(row) === 'sharing' && shareRowMatches(row, query)).length
      + model.items.filter((item) => itemMatches(item, query)).length + model.waiting.filter((item) => has(query, item.message, item.item)).length;
  }
  return filterActivity(data.activity, query, settings).length;
}
