// Pure presentation rules. Times in the agt contracts are Unix seconds.
const STATES = Object.freeze({
  off: "off", logged_out: "logged out", unhealthy: "unhealthy", cooling: "cooling",
  recovering: "recovering", busy: "busy", ok: "", unknown: "unknown",
});
const finite = (value) => typeof value === "number" && Number.isFinite(value);
const optionalTime = (value) => value === null || finite(value);
const percent = (window) => window.reset ? 0 : Math.round(Math.min(100, Math.max(0, window.used_percent)));

function normalizeAccount(account, now) {
  try {
    if (!account || typeof account.name !== "string" || typeof account.enabled !== "boolean"
        || !Object.hasOwn(STATES, account.availability) || !optionalTime(account.until)
        || !Array.isArray(account.windows)) throw new Error("Malformed account usage");
    const windows = account.windows.map((window) => {
      if (!window || typeof window.name !== "string" || typeof window.label !== "string"
          || !finite(window.used_percent) || !optionalTime(window.resets_at)
          || !optionalTime(window.observed_at) || typeof window.reset !== "boolean"
          || typeof window.stale !== "boolean") throw new Error("Malformed usage window");
      const reset = window.reset || (window.resets_at !== null && window.resets_at <= now);
      return { ...window, reset, used_percent: percent({ ...window, reset }),
        stale: !reset && window.stale && window.resets_at !== null && window.resets_at > now };
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
    return { ...provider, accounts: provider.accounts.map((account) => normalizeAccount(account, now)) };
  }) };
}

/** Equal percentages retain agt's window order; an account with no windows returns null. */
export function busierWindow(windows) {
  return windows.reduce((best, window) => !finite(window?.used_percent) ? best
    : best === null || percent(window) > percent(best) ? window : best, null);
}

export function percentText(window) {
  return window && finite(window.used_percent) ? `${percent(window)}%` : "";
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

/** Healthy rows use only their dot. Buttons and timed back text are chosen by the view. */
export function stateWord(availability) {
  return Object.hasOwn(STATES, availability) ? STATES[availability] : STATES.unknown;
}

export function backText(until, now, { timeZone } = {}) {
  if (!finite(until) || !finite(now) || until <= now) return "";
  const clock = new Intl.DateTimeFormat("en-GB", {
    timeZone, hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).format(new Date(until * 1000));
  return `back ${clock}`;
}
