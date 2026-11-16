// Pure presentation: no DOM, clock reads or IPC.
import { backText, busierWindow, clockText, decodeUsage, percentText, resetText, stateWord } from './usage.js';
const LABELS = { claude: 'Claude', codex: 'Codex', antigravity: 'Antigravity', kiro: 'Kiro' };
const NO_WINDOWS = new Set(['kiro', 'antigravity']);
export function ageText(observed, now) {
  if (!Number.isFinite(observed)) return 'age unknown';
  const minutes = Math.max(0, Math.floor((now - observed) / 60));
  if (minutes < 60) return `${minutes} m ago`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)} h ago`;
  return `${Math.floor(minutes / 1440)} d ago`;
}
export function accountRow(provider, account, now, options = {}) {
  const providerName = LABELS[provider.runtime] || provider.label;
  const windows = NO_WINDOWS.has(provider.runtime) ? [] : account.windows;
  const best = busierWindow(windows);
  const action = account.problem ? null : account.availability === 'logged_out' ? 'login'
    : account.availability === 'unknown' ? 'check' : null;
  const button = action === 'login' ? 'Log in…' : action === 'check' ? 'Check' : '';
  const back = backText(account.until, now, options);
  const capacity = Number.isFinite(account.capacity) ? account.capacity : null;
  const running = Number.isFinite(account.running_jobs) ? account.running_jobs : 0;
  const jobs = account.availability === 'busy' || running > 0 ? `${running} of ${capacity ?? 'unlimited'} jobs` : '';
  const state = ['cooling', 'unhealthy'].includes(account.availability) && back ? back
    : account.availability === 'busy' && capacity !== null ? jobs : stateWord(account.availability);
  const figure = !action && !state && best ? percentText(best) : '';
  const reset = figure ? best.stale ? ageText(best.observed_at, now) : resetText(best, now, options) : '';
  const details = windows.map((window) => {
    const when = resetText(window, now, options);
    const resetLabel = window.reset ? 'reset' : when ? `resets ${when}` : 'reset time unknown';
    return `${window.label} ${percentText(window)}, ${resetLabel}${window.stale ? `, ${ageText(window.observed_at, now)}` : ''}`;
  });
  const tooltip = [`${providerName} ${account.name}`, state || 'Available', ...details, jobs === state ? '' : jobs,
    account.problem || account.reason].filter(Boolean).join('. ');
  const verb = button.replace('…', '');
  return { key: `${provider.runtime}:${account.name}`, runtime: provider.runtime, name: account.name,
    provider: providerName, availability: account.availability, action, button, figure, reset,
    state: button ? '' : state, slot: button || [figure, reset].filter(Boolean).join(' ') || state,
    tooltip, accessibleLabel: `${tooltip}. ${button ? `${verb} for ${account.name}` : 'Open in Settings'}.`,
    linkLabel: `${tooltip}. Open in Settings.`,
    busyLabel: action === 'login' ? `Opening Terminal for ${account.name}` : action === 'check' ? `Checking ${account.name}` : '',
    tabStops: 1, stale: !!figure && best.stale, dimmed: !!figure && best.stale, threshold: !!figure && best.used_percent >= 95,
    meters: ['five_hour', 'seven_day'].map((name) => {
      const window = windows.find((item) => item.name === name);
      return { name, present: !!window, percent: window?.used_percent ?? 0, reset: window?.reset ?? false,
        dimmed: window?.stale ?? false, dominant: !!window && best === window };
    }), hasWindows: windows.length > 0 };
}
// Footer copy. The error itself is shown verbatim as the detail line when it adds something.
export function lastRow(snapshot) {
  if (snapshot.error) {
    const error = String(snapshot.error);
    if (error === 'agt not found') {
      return { kind: 'problem', text: "agt isn't installed.", detail: '', button: 'Install', action: 'settings', tooltip: error };
    }
    if (/^agt .+ is too old$/.test(error)) {
      return { kind: 'problem', text: `${error}.`, detail: '', button: 'Repair', action: 'settings', tooltip: error };
    }
    const text = "Can't read agt.";
    const same = error.replace(/\.$/, '') === text.replace(/\.$/, '');
    return { kind: 'problem', text, detail: same ? '' : error, button: 'Retry', action: 'refresh', tooltip: error };
  }
  return snapshot.update_version ? { kind: 'update', text: `Update to ${snapshot.update_version}…`, detail: '',
    button: '', action: 'update', tooltip: `Update to ${snapshot.update_version}…` } : null;
}
export const TITLE = 'Agents Router';
export function popoverModel(snapshot, now, options = {}) {
  const usage = snapshot.usage ? decodeUsage(snapshot.usage, now) : null;
  const rows = usage ? usage.providers.flatMap((provider) => provider.accounts.map((account, index) => ({
    ...accountRow(provider, account, now, options), providerFirst: index === 0 }))) : [];
  const usable = usage?.providers.some((provider) => provider.accounts.some((account) => account.enabled
    && ['ok', 'recovering'].includes(account.availability)));
  const returns = usage ? usage.providers.flatMap((provider) => provider.accounts)
    .filter((account) => account.enabled && account.until > now).map((account) => account.until) : [];
  const until = !snapshot.error && rows.length && !usable && returns.length ? clockText(Math.min(...returns), now, options) : '';
  return { rows, header: until ? `No account ready until ${until}` : TITLE, reading: !!snapshot.reading,
    empty: !!usage && !rows.length && !snapshot.error, first: !usage && !snapshot.error, footer: lastRow(snapshot) };
}
