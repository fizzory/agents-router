import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { backText, busierWindow, decodeUsage, percentText, resetText, stateWord } from "./usage.js";

// Read the producer's golden contract in each test, never a live CLI or home folder.
const fixture = () => JSON.parse(readFileSync(new URL("../../../tests/fixtures/app/usage.json", import.meta.url), "utf8"));
const accounts = (data) => data.providers[0].accounts;
const account = (data, name) => accounts(data).find((item) => item.name === `claude-${name}`);

test("decode schema 1, preserve provider/account order, and give no meters to Kiro or Antigravity", () => {
  const raw = fixture();
  const before = JSON.stringify(raw);
  const result = decodeUsage(JSON.stringify(raw));
  assert.deepEqual(result.providers.map((provider) => provider.runtime), ["claude", "codex", "antigravity", "kiro"]);
  assert.deepEqual(accounts(result).map((item) => item.name), accounts(raw).map((item) => item.name));
  assert.equal(account(result, "stale").windows[0].stale, true);
  assert.deepEqual(account(result, "reset").windows[0], {
    ...account(raw, "reset").windows[0], used_percent: 0, stale: false,
  });
  for (const provider of result.providers.slice(2)) assert.deepEqual(provider.accounts[0].windows, []);
  assert.equal(JSON.stringify(raw), before);
  assert.notEqual(accounts(result), accounts(raw));
});

test("decode rejects unknown schemas and broken snapshot/provider shapes", () => {
  const raw = fixture();
  for (const schema_version of [undefined, 0, 2, "1"]) {
    assert.throws(() => decodeUsage({ ...raw, schema_version }), /schema_version/);
  }
  assert.throws(() => decodeUsage({ ...raw, generated_at: Infinity }), /snapshot/);
  assert.throws(() => decodeUsage({ ...raw, providers: [{}] }), /provider/);
  assert.throws(() => decodeUsage("{"), SyntaxError);
});

test("normalize only the bad account to unknown, clamp meters, and expire stale windows", () => {
  const raw = fixture();
  account(raw, "work").windows[0].used_percent = NaN;
  account(raw, "stale").windows[0].used_percent = 140;
  let result = decodeUsage(raw);
  assert.equal(account(result, "work").availability, "unknown");
  assert.match(account(result, "work").problem, /window/);
  assert.deepEqual(account(result, "work").windows, []);
  assert.equal(account(result, "cooling").availability, "cooling");
  assert.equal(account(result, "stale").windows[0].used_percent, 100);
  result = decodeUsage(raw, raw.generated_at + 3600);
  assert.equal(account(result, "stale").windows[0].used_percent, 0);
  assert.equal(account(result, "stale").windows[0].stale, false);
});

test("busier window uses percent, keeps ties stable, and does not prefer a reset window", () => {
  const raw = fixture();
  const windows = account(raw, "work").windows;
  assert.equal(busierWindow(windows).name, "seven_day");
  assert.equal(busierWindow([...account(raw, "reset").windows, ...windows]), windows[1]);
  assert.equal(busierWindow(windows.map((window) => ({ ...window, used_percent: 42 }))).name, "five_hour");
  assert.equal(busierWindow(account(raw, "cooling").windows), null);
});

test("percent text shows usage, an empty reset, and no invented figure", () => {
  const raw = fixture();
  assert.equal(percentText(busierWindow(account(raw, "work").windows)), "42%");
  assert.equal(percentText(account(raw, "reset").windows[0]), "0%");
  assert.equal(percentText(busierWindow(account(raw, "cooling").windows)), "");
  const window = account(raw, "work").windows[0];
  assert.equal(percentText({ ...window, used_percent: -5 }), "0%");
  assert.equal(percentText({ ...window, used_percent: 100.5 }), "100%");
  assert.equal(percentText({ ...window, used_percent: Infinity }), "");
});

test("reset text uses local clock today, weekday later, and nothing after reset", () => {
  const raw = fixture();
  const windows = account(raw, "work").windows;
  assert.equal(resetText(windows[0], raw.generated_at, { timeZone: "UTC" }), "09:00");
  assert.equal(resetText(windows[1], raw.generated_at, { timeZone: "UTC" }), "Sat");
  assert.equal(resetText(windows[0], raw.generated_at, { timeZone: "Asia/Bangkok" }), "16:00");
  assert.equal(resetText(windows[0], windows[0].resets_at, { timeZone: "UTC" }), "");
  assert.equal(resetText(account(raw, "reset").windows[0], raw.generated_at), "");
  assert.equal(resetText(null, raw.generated_at), "");
});

test("all fixture availability states have plain row words; a healthy row has only its dot", () => {
  const expected = { off: "off", logged_out: "logged out", unhealthy: "unhealthy", cooling: "cooling",
    recovering: "recovering", busy: "busy", ok: "", unknown: "unknown" };
  const seen = new Set();
  for (const item of accounts(fixture())) {
    seen.add(item.availability);
    assert.equal(stateWord(item.availability), expected[item.availability]);
  }
  assert.deepEqual([...seen].sort(), Object.keys(expected).sort());
  assert.equal(stateWord("future-state"), "unknown");
});

test("back text uses 24-hour local time and never invents a return time", () => {
  const raw = fixture();
  const until = account(raw, "cooling").until;
  assert.equal(backText(until, raw.generated_at, { timeZone: "UTC" }), "back 08:10");
  assert.equal(backText(until, raw.generated_at, { timeZone: "Asia/Bangkok" }), "back 15:10");
  assert.equal(backText(account(raw, "recovering").until, raw.generated_at), "");
  assert.equal(backText(account(raw, "unknown").until, raw.generated_at), "");
  assert.equal(backText(until, until), "");
});
