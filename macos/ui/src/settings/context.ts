// What every pane gets from the Settings controller.
import { createContext, useContext, type MouseEvent } from 'react';
import type { RowMessage, Scope } from '../shared';
import type { ConfigRow, ConfigShow, SettingsApi, SettingsTarget } from './api';
import type { PaneId } from './model';

export interface SettingsServices {
  api: SettingsApi;
  scope: Scope;
  query: string;
  /** The key of the write in flight; every other control ignores input meanwhile. */
  busyKey: string | null;
  messages: Map<string, RowMessage>;
  setMessage(key: string, message: RowMessage | null): void;
  save(row: ConfigRow, value: string | null, options?: { remove?: boolean; document?: ConfigShow; scope?: Scope; revision?: string | null }): Promise<void>;
  /** A pane write outside session history (account toggles, picks, kinds, reverts). Reloads that pane after. */
  mutate<T>(key: string, pane: PaneId, work: () => Promise<T>): Promise<T | undefined>;
  roleWrite(roleKey: string, edit: unknown): Promise<unknown>;
  navigate(target: SettingsTarget): Promise<void>;
  showMenu(event: MouseEvent | null, actions: Record<string, () => unknown>): void;
  report(error: unknown): void;
  reload(pane: PaneId): Promise<void>;
  openAddAccount(): void;
  reduced: boolean;
  now: () => number;
  timeZone?: string;
}

export const SettingsContext = createContext<SettingsServices | null>(null);
export function useSettings(): SettingsServices {
  const value = useContext(SettingsContext);
  if (!value) throw new Error('useSettings outside SettingsApp');
  return value;
}

/** Refusal shapes from agt: {ok:false}, {error}, {needs_user_words}. */
export function refusal(result: unknown): { text: string; ask: boolean } | null {
  if (!result || typeof result !== 'object') return null;
  const value = result as { ok?: boolean; error?: unknown; needs_user_words?: unknown; exit_code?: number };
  if (value.ok !== false && !value.error && !value.needs_user_words) return null;
  const error = value.error as { needs_user_words?: unknown } | string | undefined;
  const ask = !!value.needs_user_words || value.exit_code === 3 || (typeof error === 'object' && !!error?.needs_user_words);
  return { text: '', ask };
}
