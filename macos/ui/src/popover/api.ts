// The popover's slice of tauri.js (the only window.__TAURI__ boundary). main.tsx
// passes the real adapter; tests pass a fake with the same shape.
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
  getSnapshot(): Promise<Envelope>;
  refreshUsage(): Promise<unknown>;
  hidePopover(): Promise<unknown>;
  setPopoverHeight(size: { height: number; durationMs: number; curve: number[] }): Promise<unknown>;
  openSettings(target?: { runtime?: string | null; account?: string | null; install?: boolean }): Promise<unknown>;
  loginAccount(runtime: string, account: string): Promise<unknown>;
  checkAccount(runtime: string, account: string): Promise<unknown>;
  checkForUpdates(): Promise<unknown>;
  quitApp(): Promise<unknown>;
  onUsageUpdated(handler: (envelope: Envelope) => void): Promise<Unlisten>;
  onPopoverStatus(handler: (envelope: Envelope) => void): Promise<Unlisten>;
}
