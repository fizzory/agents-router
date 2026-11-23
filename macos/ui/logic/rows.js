// Pure presentation: no DOM, clock reads or IPC.
import { backText, busierWindow, clockText, countdownText, decodeUsage, detailResetText, percentText, resetText, stateWord, withAccountCheck } from './usage.js';
const LABELS = { claude: 'Claude', codex: 'Codex', antigravity: 'Antigravity', kiro: 'Kiro' };
const NO_WINDOWS = new Set(['kiro', 'antigravity']);
export function ageText(observed, now) {
  if (!Number.isFinite(observed)) return 'age unknown';
  const minutes = Math.max(0, Math.floor((now - observed) / 60));
  if (minutes < 60) return `${minutes}m ago`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)}h ago`;
  return `${Math.floor(minutes / 1440)}d ago`;
}

function windowTimeLabel(window, stale, now, options) {
  if (window.reset) return ageText(window.observed_at, now);
  if (stale) return ageText(options.trouble ? options.observed : window.observed_at, now);
  if (options.popover === true) return countdownText(window.resets_at, now);
  return resetText(window, now, options);
}

// One rule for both the popover's marks and its summary. Leases can occupy
// more slots than there are running jobs, so running_jobs alone is not capacity.
export function readiness(account) {
  const state = account.availability;
  const running = Number.isFinite(account.running_jobs) ? account.running_jobs : 0;
  const occupied = Number.isFinite(account.busy_slots) ? account.busy_slots : running;
  const full = Number.isFinite(account.capacity) && occupied >= account.capacity;
  if (!account.enabled || state === 'off') return { shape: 'hollow', breathing: false, label: 'Off' };
  if (account.problem) return { shape: 'hollow', breathing: false, label: 'Unknown' };
  if (account.check_failed) return { shape: 'ring', breathing: false, label: 'Check failed' };
  if (account.checking) return { shape: 'hollow', breathing: false, label: 'Checking…' };
  if (['logged_out', 'unhealthy'].includes(state)) return { shape: 'ring', breathing: false, label: stateWord(state) };
  if (state === 'cooling') return { shape: 'half', breathing: false, label: 'Cooling down' };
  if (['ok', 'recovering', 'busy'].includes(state)) {
    if (full) return { shape: 'half', breathing: false, label: 'Busy, full' };
    return { shape: 'solid', breathing: running > 0 || state === 'busy',
      label: running > 0 || state === 'busy' ? 'Busy, available' : state === 'recovering' ? 'Recovering, available' : 'Available' };
  }
  return { shape: 'hollow', breathing: false, label: 'Unknown' };
}

export function accountRow(provider, account, now, options = {}) {
  const popover = options.popover === true;
  const providerName = LABELS[provider.runtime] || provider.label;
  const windows = NO_WINDOWS.has(provider.runtime) ? [] : account.windows;
  const best = busierWindow(windows);
  const action = account.problem || account.checking ? null : popover && account.check_failed ? 'retry' : account.availability === 'logged_out' ? 'login'
    : !popover && account.availability === 'unknown' ? 'check' : null;
  const button = action === 'login' ? 'Log in…' : action === 'check' ? 'Check' : action === 'retry' ? 'Retry' : '';
  const back = backText(account.until, now, options);
  const capacity = Number.isFinite(account.capacity) ? account.capacity : null;
  const running = Number.isFinite(account.running_jobs) ? account.running_jobs : 0;
  const jobs = account.availability === 'busy' || running > 0 ? `${running} of ${capacity ?? 'unlimited'} jobs` : '';
  const mark = readiness(account);
  const state = account.checking ? 'Checking…' : ['cooling', 'unhealthy', 'recovering'].includes(account.availability) && back ? back
    : (popover && ['ok', 'recovering', 'busy'].includes(account.availability) && (running > 0 || account.availability === 'busy'))
      || (account.availability === 'busy' && capacity !== null) ? jobs
    : popover && NO_WINDOWS.has(provider.runtime) && mark.shape === 'solid' && account.availability === 'ok' ? 'Ready' : stateWord(account.availability);
  const figure = !action && !state && best ? percentText(best) : '';
  const stale = !!figure && (best.reset || best.stale || !!options.trouble);
  const reset = figure ? windowTimeLabel(best, stale, now, options) : '';
  const windowTag = popover && figure && !best.reset ? best.name === 'five_hour' ? '5h' : 'wk' : '';
  const details = windows.map((window) => {
    if (window.reset) return `${window.name === 'five_hour' ? '5h' : window.name === 'seven_day' ? 'Week' : window.label} unknown since reset, last seen ${ageText(window.observed_at, now)}`;
    const when = popover ? detailResetText(window, now, options) : resetText(window, now, options);
    const resetLabel = when ? `resets ${when}` : 'reset time unknown';
    return `${window.label} ${percentText(window)}, ${resetLabel}${window.stale ? `, ${ageText(window.observed_at, now)}` : ''}`;
  });
  const tooltip = [`${providerName} ${account.name}`, popover ? mark.label : state || 'Available', ...details,
    popover && back ? back : '', popover || jobs !== state ? jobs : '',
    account.problem || account.reason].filter(Boolean).join('. ');
  const observed = windows.map(window => window.observed_at).filter(Number.isFinite);
  const updated = ageText(options.trouble ? options.observed : observed.length ? Math.min(...observed) : options.observed, now);
  const detailLines = windows.map(window => `${window.name === 'five_hour' ? '5 hours' : window.name === 'seven_day' ? 'Week' : window.label}  ${percentText(window)}  ${window.reset ? 'Reset, not seen since' : `Resets ${detailResetText(window, now, options) || 'at an unknown time'}`}`);
  const usageContext = account.statusline_usage == null || account.statusline_usage === 'live' ? ''
    : account.statusline_usage === 'no_limits' ? 'No usage limits on this login' : "Usage updates only from agt's jobs";
  const detailContext = [NO_WINDOWS.has(provider.runtime) ? 'No usage reported' : usageContext || jobs || account.problem || account.reason,
    `Updated ${updated}`].filter(Boolean).join(' · ');
  const usageSetupCommand = !popover && ['missing', 'replaced'].includes(account.statusline_usage) ? 'agt setup' : '';
  const verb = button.replace('…', '');
  return { key: `${provider.runtime}:${account.name}`, runtime: provider.runtime, name: account.name,
    provider: providerName, availability: account.availability, checking: !!account.checking, action, button, figure, reset, windowTag,
    ...mark, detailLines, detailContext, usageSetupCommand,
    state: button ? '' : state, slot: button || [windowTag, figure, reset].filter(Boolean).join(' ') || state,
    tooltip, accessibleLabel: `${tooltip}. ${button ? `${verb} for ${account.name}` : 'Open in Settings'}.`,
    linkLabel: `${tooltip}. ${popover ? 'Show account detail' : 'Open in Settings'}.`,
    busyLabel: action === 'login' ? `Opening Terminal for ${account.name}` : ['check', 'retry'].includes(action) ? `Checking ${account.name}` : '',
    tabStops: 1, stale, dimmed: !!options.trouble || (popover ? account.availability === 'off' : stale), threshold: !!figure && !best.reset && best.used_percent >= 95,
    meters: ['five_hour', 'seven_day'].map((name) => {
      const window = windows.find((item) => item.name === name);
      return { name, present: !!window, percent: window?.used_percent ?? 0, reset: window?.reset ?? false,
        dimmed: !!options.trouble || !!window?.reset || (window?.stale ?? false), dominant: !!window && best === window,
        warn: popover && name === 'five_hour' && account.availability === 'cooling' };
    }), hasWindows: windows.length > 0 };
}
// Trouble row copy. The error itself is shown verbatim as the detail line when it adds something.
export function lastRow(snapshot, now, observed) {
  if (snapshot.error) {
    const error = String(snapshot.error);
    const age = ageText(observed, now).replace(/(\d+)([mhd]) ago/, (_, number, unit) =>
      `${number} ${{ m: 'minute', h: 'hour', d: 'day' }[unit]}${number === '1' ? '' : 's'} ago`);
    const detail = Number.isFinite(observed) ? `${error.replace(/[.\s]+$/, '')}. The numbers below are from ${age}.` : error;
    if (error === 'agt not found') {
      return { kind: 'problem', text: "agt isn't installed", detail, button: 'Install…', action: 'settings', tooltip: error };
    }
    if (/^agt .+ is too old$/.test(error)) {
      return { kind: 'problem', text: error, detail, button: 'Repair…', action: 'settings', tooltip: error };
    }
    return { kind: 'problem', text: "Can't read agt", detail, button: 'Retry', action: 'refresh', tooltip: error };
  }
  return null;
}
const megabytes = (bytes) => (bytes / 1e6).toFixed(1);
// Download progress for the Installing… row: decimal megabytes, blank until bytes arrive.
export function progressText(received, total) {
  if (!Number.isFinite(received) || received <= 0) return '';
  return Number.isFinite(total) && total > 0 ? `${megabytes(Math.min(received, total))} of ${megabytes(total)} MB` : `${megabytes(received)} MB`;
}
// The update row from Rust's update status. Phase and retryable set the title,
// as Dialog::failure in update.rs does; retryable alone sets the button.
export function updateRow(status) {
  const version = status?.version;
  if (!version) return null;
  if (status.phase === 'available') {
    return { kind: 'available', text: `${version} is available`, button: 'Install and Restart', action: 'install', detail: '', progress: '' };
  }
  if (status.phase === 'downloading' || status.phase === 'installing') {
    return { kind: 'installing', text: 'Installing…', button: '', action: null, detail: '',
      progress: status.phase === 'downloading' ? progressText(status.received, status.total) : '' };
  }
  if (status.phase === 'download-failed' || status.phase === 'install-failed') {
    const verb = status.phase === 'install-failed' ? 'install' : status.retryable ? 'download' : 'verify';
    return { kind: 'failed', text: `Couldn't ${verb} ${version}.`, button: status.retryable ? 'Retry' : 'Open Releases Page',
      action: status.retryable ? 'install' : 'releases', detail: status.detail || '', progress: '' };
  }
  return null;
}
export const TITLE = 'Agents Router';
export function popoverModel(snapshot, now, options = {}) {
  const usage = snapshot.usage ? decodeUsage(snapshot.usage, now) : null;
  const observed = snapshot.fetched_at ?? usage?.generated_at;
  const rows = usage ? usage.providers.flatMap((provider) => provider.accounts.map((account, index) => ({
    ...accountRow(provider, withAccountCheck(account, snapshot.account_checks?.find(check => check.runtime === provider.runtime && check.account === account.name), true), now, { ...options, popover: true, trouble: !!snapshot.error, observed }), providerFirst: index === 0 }))) : [];
  const ready = rows.filter(row => row.shape === 'solid').length;
  const returns = usage ? usage.providers.flatMap((provider) => provider.accounts)
    .filter((account) => account.enabled && account.until > now).map((account) => account.until) : [];
  const until = returns.length ? clockText(Math.min(...returns), now, options) : '';
  const last = lastRow(snapshot, now, usage ? observed : null);
  // Trouble hides only an available update; Installing… and failures stay.
  const update = updateRow(snapshot.update);
  return { rows, ready, header: last ? last.text : !usage || !rows.length ? TITLE : ready ? `${ready} ready`
    : rows.some(row => row.checking) ? 'Checking accounts…' : until ? `None ready until ${until}` : 'None ready', reading: !!snapshot.reading,
    freshness: usage ? `Updated ${ageText(observed, now)}` : '',
    empty: !!usage && !rows.length && !snapshot.error, first: !usage && !snapshot.error, footer: last,
    update: last && update?.kind === 'available' ? null : update };
}
