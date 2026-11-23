export interface AccountCheck {
  runtime: string;
  account: string;
  phase: 'idle' | 'checking' | 'complete' | 'failed';
  result: { account: string; logged_in: boolean | null; checked_at: number; error: string | null } | null;
}

// update_app.rs's status, sent to both windows. Settings shows `dialog`; the
// popover's update row reads the rest.
export interface UpdateDialog { title: string; detail: string; buttons: [string, string][]; default: string; busy: boolean }
export type UpdatePhase = 'idle' | 'available' | 'downloading' | 'installing' | 'download-failed' | 'install-failed';
export interface UpdateStatus {
  dialog: UpdateDialog | null;
  received?: number | null;
  total?: number | null;
  version?: string | null;
  phase?: UpdatePhase;
  retryable?: boolean;
  /** Rust's error text for a failed install: a tooltip, never inline. */
  detail?: string;
}

// The row model logic/rows.js builds (accountRow). Times are already formatted.
export interface LoginResult {
  status: 'opened' | 'terminal_failed';
  copy_command: string;
}

export interface Meter {
  name: string;
  present: boolean;
  percent: number;
  reset: boolean;
  dimmed: boolean;
  dominant: boolean;
  warn?: boolean;
}

export interface RowModel {
  key: string;
  runtime: string;
  name: string;
  provider: string;
  providerFirst?: boolean;
  availability: string;
  action: 'login' | 'check' | 'retry' | null;
  checking?: boolean;
  button: string;
  figure: string;
  reset: string;
  windowTag: string;
  shape: 'solid' | 'half' | 'ring' | 'hollow';
  breathing: boolean;
  label: string;
  detailLines: string[];
  detailContext: string;
  usageSetupCommand: string;
  state: string;
  slot: string;
  tooltip: string;
  accessibleLabel: string;
  linkLabel: string;
  busyLabel: string;
  stale: boolean;
  dimmed: boolean;
  threshold: boolean;
  meters: Meter[];
  hasWindows: boolean;
}
