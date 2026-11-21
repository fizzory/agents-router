import type { AccountCheck } from '../shared/types';
// The popover's slice of tauri.js (the only window.__TAURI__ boundary). main.tsx
// passes the real adapter; tests pass a fake with the same shape.
import type { LoginResult, UpdateStatus } from '../shared/types';
export interface Envelope {
  usage: unknown;
  error: string | null;
  reading: boolean;
  generation: number;
  fetched_at?: number | null;
  update_version: string | null;
}

export type Unlisten = () => void;

export interface PopoverApi {
  getAccountChecks(): Promise<AccountCheck[]>;
  onAccountChecksStatus(handler: (checks: AccountCheck[]) => void): Promise<Unlisten>;
  getSnapshot(): Promise<Envelope>;
  refreshUsage(): Promise<unknown>;
  hidePopover(): Promise<unknown>;
  showPopoverMenu(position: { x: number; y: number }): Promise<void>;
  setPopoverHeight(size: { height: number; durationMs: number; curve: number[] }): Promise<unknown>;
  openSettings(target?: { runtime?: string | null; account?: string | null; install?: boolean }): Promise<unknown>;
  loginAccount(runtime: string, account: string): Promise<LoginResult>;
  getLoginCommand(runtime: string, account: string): Promise<string>;
  copyText(text: string): Promise<unknown>;
  checkAccount(runtime: string, account: string): Promise<unknown>;
  getUpdateStatus(): Promise<UpdateStatus>;
  onUpdateStatus(handler: (status: UpdateStatus) => void): Promise<Unlisten>;
  /** Install and Restart, and Retry. Shares Settings' install guard and never opens Settings. */
  installUpdate(): Promise<void>;
  /** Opens /releases/latest in the browser, and nothing else. */
  openReleases(): Promise<unknown>;
  quitApp(): Promise<unknown>;
  onUsageUpdated(handler: (envelope: Envelope) => void): Promise<Unlisten>;
  onPopoverStatus(handler: (envelope: Envelope) => void): Promise<Unlisten>;
}
