import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { decodeUsage } from './usage.js';
import { accountRow, ageText, lastRow, popoverModel, progressText, readiness, updateRow } from './rows.js';
const load = (name) => JSON.parse(readFileSync(new URL(`../../../tests/fixtures/${name}`, import.meta.url), 'utf8'));
const fixture = () => load('app/usage-popover.json');
const options = { timeZone: 'UTC' };
const row = (name, data = fixture(), now = data.generated_at) => {
  const decoded = decodeUsage(data, now), provider = decoded.providers[0];
  return accountRow(provider, provider.accounts.find((account) => account.name === `claude-${name}`), now, options);
};

test('S1: canonical name, fixed status text, and both windows with resets in the accessible label', () => {
  const shown = row('work');
  assert.equal(shown.provider, 'Claude');
  assert.equal(shown.name, 'claude-work');
  assert.equal(shown.slot, '86% Wed');
  assert.equal(shown.state, '');
  assert.equal(shown.meters[0].dominant, false);
  assert.equal(shown.meters[1].dominant, true);
  assert.match(shown.accessibleLabel, /Claude claude-work\. Available\. 5h 17%, resets 20:00\. week 86%, resets Wed/);
  assert.equal(shown.tabStops, 1);
});

test('S2: each generated availability state has its decided row text and accessible reason', () => {
  const expected = { cooling: 'Back 21:40', 'logged-out': 'Log in…', unknown: 'Check', busy: '2 of 4 jobs', off: 'Off',
    unhealthy: 'Back 19:02', recovering: 'Recovering', malformed: 'Unknown' };
  for (const [name, text] of Object.entries(expected)) {
    const shown = row(name);
    assert.equal(shown.slot, text, name);
    assert.equal(shown.tabStops, 1);
    assert.match(shown.accessibleLabel, new RegExp(`claude-${name}`));
    if (shown.button) assert.ok(shown.accessibleLabel.includes(shown.button.replace('…', '')));
    else assert.ok(shown.accessibleLabel.includes(text));
  }
  assert.equal(row('logged-out').action, 'login');
  assert.equal(row('unknown').action, 'check');
  assert.equal(row('malformed').action, null);
  assert.match(row('malformed').tooltip, /malformed usage windows/);
  assert.match(row('busy').tooltip, /2 of 4 jobs/);
  assert.equal(row('busy').slot, '2 of 4 jobs');
  assert.equal((row('busy').tooltip.match(/2 of 4 jobs/g) || []).length, 1);
  assert.equal(row('logged-out').busyLabel, 'Opening Terminal for claude-logged-out');
  assert.equal(row('unknown').busyLabel, 'Checking claude-unknown');
  assert.match(row('work').linkLabel, /\. Open in Settings\.$/);
  assert.match(row('cooling').tooltip, /usage limit/);
});

test('S1/S2 stale dimming belongs to each window only until its reset, with its own age', () => {
  const data = fixture();
  const stale = row('stale', data);
  assert.equal(stale.slot, '86% 2h ago');
  assert.equal(stale.dimmed, true);
  assert.match(stale.accessibleLabel, /5h 70%, resets 20:00, 1h ago\. week 86%, resets Wed, 2h ago/);
  const expired = row('stale', data, data.generated_at + 3600);
  assert.equal(expired.meters[0].percent, 0);
  assert.equal(expired.meters[0].dimmed, false);
  assert.equal(expired.meters[1].dimmed, true);
  const allExpired = row('stale', data, data.generated_at + 86400);
  assert.equal(allExpired.dimmed, false);
  assert.ok(allExpired.meters.every((meter) => meter.percent === 0 && !meter.dimmed));
  assert.doesNotMatch(allExpired.accessibleLabel, /ago/);
});

test('S2 reset wins over stale: empty bar, no age, other window and reset remain accessible', () => {
  const shown = row('reset');
  assert.equal(shown.slot, '50% Wed');
  assert.equal(shown.meters[0].percent, 0);
  assert.equal(shown.meters[0].reset, true);
  assert.equal(shown.meters[0].dimmed, false);
  assert.match(shown.accessibleLabel, /5h 0%, reset\. week 50%, resets Wed/);
  assert.doesNotMatch(shown.accessibleLabel, /ago/);
});

test('S1/S2 no figure for Kiro and Antigravity; healthy means a dot only', () => {
  const data = decodeUsage(fixture());
  for (const provider of data.providers.filter((item) => ['kiro', 'antigravity'].includes(item.runtime))) {
    const account = provider.accounts[0];
    const shown = accountRow(provider, account, data.generated_at, options);
    assert.equal(shown.availability, 'ok');
    assert.equal(shown.slot, '');
    assert.equal(shown.figure, '');
    assert.equal(shown.hasWindows, false);
    assert.match(shown.accessibleLabel, /Available/);
    // Even an invalid upstream figure must never invent usage for these providers.
    assert.equal(accountRow(provider, { ...account, windows: data.providers[0].accounts[0].windows }, data.generated_at).hasWindows, false);
  }
});

test('S2h/S6: checking takes priority, then nothing usable names the earliest return', () => {
  const data = load('app/usage-nothing.json');
  assert.equal(popoverModel({ usage: data }, data.generated_at, options).header, 'Checking accounts…');
  data.providers.forEach(provider => { provider.accounts = provider.accounts.filter(account => account.availability !== 'unknown'); });
  assert.equal(popoverModel({ usage: data }, data.generated_at, options).header, 'None ready until 20:15');
  assert.equal(popoverModel({ usage: data, error: 'agt not found' }, data.generated_at, options).header, "agt isn't installed");
});

test('S1d/S6: generated native envelopes preserve the last snapshot, first read, empty/error actions, and footer priority', () => {
  const states = load('popover/states.json'), data = fixture();
  const models = Object.fromEntries(Object.entries(states).map(([key, state]) => {
    const usage = state.fixture ? load(`app/${state.fixture}`) : state.empty ? { ...data, providers: [] } : null;
    const update = state.update_version ? { version: state.update_version, phase: 'available' } : null;
    return [key, popoverModel({ ...state, usage, update }, data.generated_at, options)];
  }));
  assert.equal(models.reading.reading, true);
  assert.equal(models.reading.rows[0].slot, 'wk 86% 1d');
  assert.equal(models.first.first, true);
  assert.equal(models.empty.empty, true);
  assert.equal(models.unreadable.footer.text, "Can't read agt");
  assert.equal(models.unreadable.footer.detail, "Can't read agt: timed out");
  assert.equal(models.unreadable.footer.button, 'Retry');
  for (const [name, text, button] of [['missing', "agt isn't installed", 'Install…'], ['old', 'agt 0.16 is too old', 'Repair…']]) {
    assert.equal(models[name].footer.text, text);
    assert.equal(models[name].footer.button, button);
    assert.equal(models[name].update, null, `${name}: trouble hides an available update`);
  }
  assert.equal(models.update.footer, null);
  assert.deepEqual([models.update.update.text, models.update.update.button, models.update.update.action],
    ['0.17.1 is available', 'Install and Restart', 'install']);
  assert.equal(models.update.header, '6 ready', 'an available update leaves the summary alone');
  assert.equal(lastRow({ error: "Can't read agt." }).detail, "Can't read agt.");
  assert.equal(lastRow({}), null);
  assert.equal(ageText(null, data.generated_at), 'age unknown');
});

test('S9/S10: the update row follows Rust status, and its failures match Dialog::failure in all four cases', () => {
  // update.rs reads the same file: popover_failure_titles_and_buttons_come_from_dialog_failure.
  const cases = load('popover/update-failures.json');
  assert.equal(cases.length, 4);
  for (const { phase, retryable, version, title, button } of cases) {
    const shown = updateRow({ version, phase, retryable, detail: 'signature mismatch', received: 0, total: null });
    assert.equal(shown.kind, 'failed');
    assert.equal(shown.text, title, `${phase} retryable=${retryable}`);
    assert.equal(shown.button, button);
    assert.equal(shown.action, retryable ? 'install' : 'releases');
    assert.equal(shown.detail, 'signature mismatch');
  }
  assert.equal(updateRow({ version: '0.17.1', phase: 'downloading', received: 0, total: 45_600_000 }).progress, '');
  for (const phase of ['downloading', 'installing']) {
    const shown = updateRow({ version: '0.17.1', phase, received: 12_340_000, total: 45_600_000 });
    assert.deepEqual([shown.kind, shown.text, shown.button, shown.action], ['installing', 'Installing…', '', null]);
  }
  assert.equal(updateRow({ version: '0.17.1', phase: 'downloading', received: 12_340_000, total: 45_600_000 }).progress, '12.3 of 45.6 MB');
  assert.equal(progressText(3_060_000, null), '3.1 MB');
  assert.equal(progressText(50_000_000, 45_600_000), '45.6 of 45.6 MB');
  for (const status of [null, {}, { phase: 'idle', version: null }, { phase: 'available', version: null }, { phase: 'idle', version: '0.17.1' }]) {
    assert.equal(updateRow(status), null);
  }
  // Trouble hides only the available row; Installing… and failures stay.
  const trouble = (phase) => popoverModel({ error: 'agt not found', update: { version: '0.17.1', phase, retryable: true } }, 0, options).update;
  assert.equal(trouble('available'), null);
  assert.equal(trouble('downloading').text, 'Installing…');
  assert.equal(trouble('install-failed').text, "Couldn't install 0.17.1.");
});

test('S1: summary counts solid marks and breath follows capacity in every running-job state', () => {
  const data = fixture(), provider = data.providers[0], template = provider.accounts[0];
  for (const availability of ['ok', 'busy', 'recovering', 'cooling', 'unhealthy', 'logged_out', 'off', 'unknown']) {
    for (const capacity of [null, 4]) for (const busy_slots of [2, 4]) {
      const account = { ...template, availability, running_jobs: 2, busy_slots, capacity, enabled: availability !== 'off' };
      const model = popoverModel({ usage: { ...data, providers: [{ ...provider, accounts: [account] }] } }, data.generated_at, options);
      const ready = ['ok', 'busy', 'recovering'].includes(availability) && (capacity === null || busy_slots < capacity);
      assert.equal(model.ready, Number(ready), `${availability}/${capacity}/${busy_slots}`);
      assert.equal(model.ready, model.rows.filter(row => row.shape === 'solid').length);
      assert.equal(model.rows[0].breathing, ready);
      assert.equal(model.rows[0].shape, readiness(account).shape);
      if (ready) assert.equal(model.header, '1 ready');
      else if (['ok', 'busy', 'recovering'].includes(availability)) assert.equal(model.rows[0].shape, 'half');
    }
  }
  assert.equal(readiness({ ...template, problem: 'bad data' }).shape, 'hollow');
});

test('S1/S3: popover tags, countdowns, warning bars, ready words and detail include reset clocks', () => {
  const data = fixture();
  const model = popoverModel({ usage: data }, data.generated_at, options);
  const work = model.rows[0];
  assert.equal(model.header, '6 ready');
  assert.equal(work.slot, 'wk 86% 1d');
  assert.deepEqual(work.detailLines, ['5 hours  17%  Resets 20:00', 'Week  86%  Resets Wed 19:00']);
  assert.equal(work.detailContext, 'Updated 1m ago');
  assert.equal(model.rows.find(row => row.runtime === 'kiro').slot, 'Ready');
  assert.equal(model.rows.find(row => row.runtime === 'antigravity').detailContext, 'No usage reported · Updated 0m ago');
  assert.equal(model.rows.find(row => row.name === 'claude-unknown').action, null);
  const decoded = decodeUsage(data), provider = decoded.providers[0];
  const cooling = accountRow(provider, { ...provider.accounts[0], availability: 'cooling' }, data.generated_at, { ...options, popover: true });
  assert.equal(cooling.meters[0].warn, true);
});

test('S4/S10: trouble dims retained rows and replaces countdowns with ageText, including 2h ago', () => {
  const data = fixture(), observed = data.generated_at - 7200;
  const model = popoverModel({ usage: data, error: 'Timed out.', fetched_at: observed }, data.generated_at, options);
  assert.equal(model.header, "Can't read agt");
  assert.equal(model.footer.detail, 'Timed out. The numbers below are from 2 hours ago.');
  assert.ok(model.rows.every(row => row.dimmed));
  assert.equal(model.rows[0].slot, 'wk 86% 2h ago');
  assert.ok(model.rows[0].meters.every(meter => meter.dimmed));
  assert.equal(model.rows[1].slot, 'Back 21:40');
  assert.equal(ageText(observed, data.generated_at), '2h ago');
  assert.equal(ageText(data.generated_at - 120, data.generated_at), '2m ago');
  assert.equal(ageText(data.generated_at - 3 * 86400, data.generated_at), '3d ago');
});

test('S6: shared snapshots distinguish checking, failure, logout and completion without a popover Check button', () => {
  const data = fixture();
  const key = 'claude:claude-unknown';
  const snapshot = { runtime: 'claude', account: 'claude-unknown', phase: 'checking', result: null };
  const model = () => popoverModel({ usage: data, account_checks: [snapshot] }, data.generated_at, options);
  const shown = () => model().rows.find(row => row.key === key);
  assert.equal(shown().slot, 'Checking…');
  assert.equal(shown().action, null);
  assert.equal(shown().shape, 'hollow');
  snapshot.phase = 'failed';
  snapshot.result = { account: snapshot.account, logged_in: null, checked_at: data.generated_at, error: 'fake check failed' };
  assert.equal(shown().button, 'Retry');
  assert.equal(shown().action, 'retry');
  assert.equal(shown().shape, 'ring');
  assert.match(shown().linkLabel, /Check failed/);
  snapshot.phase = 'checking';
  assert.equal(shown().button, '');
  assert.equal(shown().slot, 'Checking…');
  snapshot.phase = 'complete';
  snapshot.result = { ...snapshot.result, logged_in: false, error: null };
  assert.equal(shown().button, 'Log in…');
  assert.equal(shown().shape, 'ring');
  snapshot.result.logged_in = true;
  assert.equal(shown().shape, 'solid');
  assert.equal(shown().button, '');
  assert.ok(model().rows.every(row => row.button !== 'Check'));
});

test('S6: failed checks never add an action to malformed or off rows', () => {
  const data = fixture();
  const checks = ['claude-malformed', 'claude-off'].map(account => ({ runtime: 'claude', account, phase: 'failed', result: { error: 'failed' } }));
  const rows = popoverModel({ usage: data, account_checks: checks }, data.generated_at).rows;
  for (const account of checks) assert.equal(rows.find(row => row.name === account.account).action, null);
});

test('S6: a completed indeterminate check with unknown usage never offers Check in the popover', () => {
  const data = fixture();
  const snapshot = { runtime: 'claude', account: 'claude-unknown', phase: 'complete',
    result: { account: 'claude-unknown', logged_in: null, checked_at: data.generated_at, error: null } };
  const shown = popoverModel({ usage: data, account_checks: [snapshot] }, data.generated_at, options)
    .rows.find(row => row.key === 'claude:claude-unknown');
  assert.equal(shown.availability, 'unknown');
  assert.equal(shown.action, null);
  assert.equal(shown.button, '');
  // The same state in Settings keeps its explicit Check affordance.
  const provider = decodeUsage(data).providers[0];
  const account = provider.accounts.find(account => account.name === snapshot.account);
  assert.equal(accountRow(provider, account, data.generated_at, options).button, 'Check');
});
