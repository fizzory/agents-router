// The row model logic/rows.js builds (accountRow). Times are already formatted.
export interface Meter {
  name: string;
  present: boolean;
  percent: number;
  reset: boolean;
  dimmed: boolean;
  dominant: boolean;
}

export interface RowModel {
  key: string;
  runtime: string;
  name: string;
  provider: string;
  providerFirst?: boolean;
  availability: string;
  action: 'login' | 'check' | null;
  button: string;
  figure: string;
  reset: string;
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
