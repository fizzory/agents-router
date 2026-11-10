import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { decodeUsage } from './usage.js';
import { accountRow, ageText, lastRow, popoverModel } from './rows.js';
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
  assert.match(shown.accessibleLabel, /Claude claude-work\. available\. 5h 17%, resets 20:00\. week 86%, resets Wed/);
  assert.equal(shown.tabStops, 1);
});

test('S2: each generated availability state has its decided row text and accessible reason', () => {
  const expected = { cooling: 'back 21:40', 'logged-out': 'Log in', unknown: 'Check', busy: 'busy', off: 'off',
    unhealthy: 'back 19:02', recovering: 'recovering', malformed: 'unknown' };
  for (const [name, text] of Object.entries(expected)) {
    const shown = row(name);
    assert.equal(shown.slot, text, name);
    assert.equal(shown.tabStops, 1);
    assert.match(shown.accessibleLabel, new RegExp(`claude-${name}`));
    if (shown.button) assert.ok(shown.accessibleLabel.includes(shown.button));
    else assert.ok(shown.accessibleLabel.includes(text));
  }
  assert.equal(row('logged-out').action, 'login');
  assert.equal(row('unknown').action, 'check');
  assert.equal(row('malformed').action, null);
  assert.match(row('malformed').tooltip, /malformed usage windows/);
  assert.match(row('busy').tooltip, /2 of 4 jobs/);
  assert.equal(row('busy').slot, 'busy');
  assert.match(row('cooling').tooltip, /usage limit/);
});

test('S1/S2 stale dimming belongs to each window only until its reset, with its own age', () => {
  const data = fixture();
  const stale = row('stale', data);
  assert.equal(stale.slot, '86% 2 h ago');
  assert.equal(stale.dimmed, true);
  assert.match(stale.accessibleLabel, /5h 70%, resets 20:00, 1 h ago\. week 86%, resets Wed, 2 h ago/);
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
    assert.match(shown.accessibleLabel, /available/);
    // Even an invalid upstream figure must never invent usage for these providers.
    assert.equal(accountRow(provider, { ...account, windows: data.providers[0].accounts[0].windows }, data.generated_at).hasWindows, false);
  }
});

test('S2h: nothing usable names the earliest return in the header', () => {
  const data = load('app/usage-nothing.json');
  assert.equal(popoverModel({ usage: data }, data.generated_at, options).header, 'Back 20:15');
  assert.equal(popoverModel({ usage: data, error: 'agt not found' }, data.generated_at, options).header, 'Agents Router');
});

test('S1d/S6: generated native envelopes preserve the last snapshot, first read, empty/error actions, and footer priority', () => {
  const states = load('popover/states.json'), data = fixture();
  const models = Object.fromEntries(Object.entries(states).map(([key, state]) => {
    const usage = state.fixture ? load(`app/${state.fixture}`) : state.empty ? { ...data, providers: [] } : null;
    return [key, popoverModel({ ...state, usage }, data.generated_at, options)];
  }));
  assert.equal(models.reading.reading, true);
  assert.equal(models.reading.rows[0].slot, '86% Wed');
  assert.equal(models.first.first, true);
  assert.equal(models.empty.empty, true);
  assert.equal(models.unreadable.footer.text, "Can't read agt");
  assert.equal(models.unreadable.footer.button, 'Retry');
  for (const [name, text, button] of [['missing', 'agt not found', 'Install'], ['old', 'agt 0.16 is too old', 'Repair']]) {
    assert.equal(models[name].footer.text, text);
    assert.equal(models[name].footer.button, button);
    assert.doesNotMatch(JSON.stringify(models[name].footer), /Update to/);
  }
  assert.equal(models.update.footer.text, 'Update to 0.17.1');
  assert.equal(lastRow({}), null);
  assert.equal(ageText(null, data.generated_at), 'age unknown');
});
