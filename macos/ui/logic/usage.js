// Pure presentation rules. Times in the agt contracts are Unix seconds.
// Frontend-owned words, shared by the popover and Settings > Accounts.
const STATES = Object.freeze({
  off: "Off", logged_out: "Logged out", unhealthy: "Unhealthy", cooling: "Cooling down",
  recovering: "Recovering", busy: "Busy", ok: "", unknown: "Unknown",
});
const finite = (value) => typeof value === "number" && Number.isFinite(value);
const optionalTime = (value) => value === null || finite(value);
const percent = (window) => window.reset ? 0 : Math.round(Math.min(100, Math.max(0, window.used_percent)));
// Match usage_view.STALE_AFTER_SECONDS, including redraws between reads.
const STALE_AFTER_SECONDS = 30 * 60;

function normalizeAccount(account, now, runtime) {
  const claude = runtime === 'claude';
  try {
    if (!account || typeof account.name !== "string" || typeof account.enabled !== "boolean"
        || !Object.hasOwn(STATES, account.availability) || !optionalTime(account.until)
        || !Array.isArray(account.windows)) throw new Error("Malformed account usage");
    const windows = account.windows.map((window) => {
      if (!window || typeof window.name !== "string" || typeof window.label !== "string"
          || !finite(window.used_percent) || !optionalTime(window.resets_at)
          || !(optionalTime(window.observed_at) || (claude && window.observed_at === undefined)) || typeof window.reset !== "boolean"
          || typeof window.stale !== "boolean") throw new Error("Malformed usage window");
      const reset = window.reset || (window.resets_at !== null && window.resets_at <= now);
      // Keep the numeric contract; reset marks an unknown value, never an observed 0%.
      return { ...window, reset, used_percent: percent({ ...window, reset }),
        stale: !reset && (claude
          ? window.stale || !finite(window.observed_at) || now - window.observed_at > STALE_AFTER_SECONDS
          : window.stale && window.resets_at !== null && window.resets_at > now) };
    });
    return { ...account, windows };
  } catch (error) {
    // A bad account must not hide the other providers or offer a misleading Check action.
    return { ...account, availability: "unknown", windows: [], until: null,
      problem: account?.problem || error.message };
  }
}

/** Decode schema 1 and copy/normalize windows without changing the received snapshot. */
export function decodeUsage(input, now) {
  const data = typeof input === "string" ? JSON.parse(input) : input;
  if (!data || data.schema_version !== 1) throw new Error("Unsupported usage schema_version (expected 1)");
  now = now ?? data.generated_at;
  if (!finite(data.generated_at) || !finite(now) || !Array.isArray(data.providers)) {
    throw new Error("Malformed usage snapshot");
  }
  return { ...data, providers: data.providers.map((provider) => {
    if (!provider || typeof provider.runtime !== "string" || typeof provider.label !== "string"
        || !Array.isArray(provider.accounts)) throw new Error("Malformed usage provider");
    return { ...provider, accounts: provider.accounts.map((account) => normalizeAccount(account, now, provider.runtime)) };
  }) };
}

/** Known windows win over reset windows; equal percentages retain agt's order. */
export function busierWindow(windows) {
  return windows.reduce((best, window) => !finite(window?.used_percent) ? best
    : best === null || (best.reset && !window.reset)
      || (!!best.reset === !!window.reset && percent(window) > percent(best)) ? window : best, null);
}

export function percentText(window) {
  return window && finite(window.used_percent) ? window.reset ? '–' : `${percent(window)}%` : "";
}

/** Today uses a clock; a later day uses its weekday. Reset windows have no future reset text. */
export function resetText(window, now, { locale = "en-GB", timeZone } = {}) {
  if (!window || window.reset || !finite(window.resets_at) || !finite(now) || window.resets_at <= now) return "";
  const when = new Date(window.resets_at * 1000);
  const today = new Date(now * 1000);
  const day = new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" });
  const options = day.format(when) === day.format(today)
    ? { hour: "2-digit", minute: "2-digit", hourCycle: "h23" } : { weekday: "short" };
  return new Intl.DateTimeFormat(locale, { ...options, timeZone }).format(when);
}

/** Detail always includes a clock, including resets on a later day. */
export function detailResetText(window, now, options = {}) {
  const when = resetText(window, now, options);
  if (!when || when.includes(':')) return when;
  return `${when} ${clockText(window.resets_at, now, options)}`;
}

/** Compact time remaining, rather than a weekday or a clock. */
export function countdownText(until, now) {
  if (!finite(until) || !finite(now) || until <= now) return '';
  const minutes = Math.floor((until - now) / 60);
  if (!minutes) return '<1m';
  if (minutes < 60) return `${minutes}m`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)}h${minutes % 60 ? ` ${minutes % 60}m` : ''}`;
  const hours = Math.floor(minutes / 60) % 24;
  return `${Math.floor(minutes / 1440)}d${hours ? ` ${hours}h` : ''}`;
}

/** Healthy rows use only their dot. Buttons and timed back text are chosen by the view. */
export function stateWord(availability) {
  return Object.hasOwn(STATES, availability) ? STATES[availability] : STATES.unknown;
}

/** A future return time as a 24-hour clock ("14:20"); empty when it has passed. */
export function clockText(until, now, { timeZone } = {}) {
  if (!finite(until) || !finite(now) || until <= now) return "";
  return new Intl.DateTimeFormat("en-GB", {
    timeZone, hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).format(new Date(until * 1000));
}

export function backText(until, now, options = {}) {
  const clock = clockText(until, now, options);
  return clock ? `Back ${clock}` : "";
}

/** Shared retained login results; Settings keeps its existing Check presentation. */
export function withAccountCheck(account, check, popover = false) {
  if (!account.enabled || account.problem) return account;
  const result = check?.result;
  const failed = check?.phase === 'failed';
  const checking = check?.phase === 'checking' || (popover && (!check || check.phase === 'idle') && account.availability === 'unknown');
  const availability = failed ? 'unknown' : result?.logged_in === false ? 'logged_out'
    : result?.logged_in === true && ['unknown', 'logged_out'].includes(account.availability) ? 'ok' : account.availability;
  return { ...account, availability, reason: failed ? result?.error : account.reason,
    check_failed: popover && failed, checking: popover && checking };
}
