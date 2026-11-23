import assert from 'node:assert/strict';
import test from 'node:test';
import { busierWindow, decodeUsage } from './usage.js';
import { accountRow, popoverModel } from './rows.js';

// The producer's claude-ntb shape after an unobserved reset. No live homes or CLI.
const now = 1_800_000_000;
const seen = now - 20 * 86400;
const rawAccount = () => ({ name: 'claude-ntb', enabled: true, availability: 'ok',
  reason: null, problem: null, until: null, running_jobs: 0, busy_slots: 0, capacity: null,
  windows: ['five_hour', 'seven_day'].map((name, index) => ({ name, label: index ? 'week' : '5h',
    used_percent: 0, observed_at: seen, resets_at: seen + (index ? 7 * 86400 : 5 * 3600),
    reset: true, stale: false })) });
const snapshot = (account = rawAccount()) => ({ schema_version: 1, generated_at: now,
  providers: [{ runtime: 'claude', label: 'Claude', accounts: [account] }] });
const settingsRow = (account = rawAccount(), time = now) => {
  const data = decodeUsage(snapshot(account), time), provider = data.providers[0];
  return accountRow(provider, provider.accounts[0], time, { timeZone: 'UTC' });
};

test('S3: claude-ntb is unknown 20d after reset in popover and Settings, never 0%', () => {
  const popover = popoverModel({ usage: snapshot() }, now).rows[0];
  const settings = settingsRow();
  for (const row of [popover, settings]) {
    assert.equal(row.slot, '– 20d ago');
    assert.equal(row.figure, '–');
    assert.equal(row.reset, '20d ago');
    assert.equal(row.windowTag, '');
    assert.equal(row.stale, true); // The figure dims even though each producer stale flag is false.
    assert.ok(row.meters.every(meter => meter.reset && meter.dimmed && meter.percent === 0));
    assert.deepEqual(row.detailLines, ['5 hours  –  Reset, not seen since', 'Week  –  Reset, not seen since']);
    assert.equal(row.detailContext, 'Updated 20d ago');
    assert.equal(row.accessibleLabel, 'Claude claude-ntb. Available. 5h unknown since reset, last seen 20d ago. Week unknown since reset, last seen 20d ago. Open in Settings.');
    assert.doesNotMatch([row.slot, row.tooltip, ...row.detailLines].join(' '), /0%/);
  }
  assert.equal(settings.dimmed, true);
});

test('S3: known windows win over unknown reset windows even when the known value is 0%', () => {
  for (const index of [0, 1]) for (const used_percent of [0, 43]) {
    const account = rawAccount();
    account.windows[index] = { ...account.windows[index], reset: false, stale: true,
      resets_at: now + 5 * 3600, observed_at: now - 3 * 3600, used_percent };
    const decoded = decodeUsage(snapshot(account)).providers[0].accounts[0];
    assert.equal(busierWindow(decoded.windows), decoded.windows[index]);
    assert.equal(busierWindow([...decoded.windows].reverse()), decoded.windows[index]);
    const row = popoverModel({ usage: snapshot(account) }, now).rows[0];
    assert.equal(row.slot, `${index ? 'wk' : '5h'} ${used_percent}% 3h ago`);
    assert.equal(row.meters[index].dominant, true);
    assert.equal(row.meters[1 - index].dimmed, true);
  }
});

test('S3: redraw recomputes unknown at the reset boundary and keeps the last observation age', () => {
  const account = rawAccount();
  account.windows = [{ ...account.windows[0], reset: false, stale: false,
    used_percent: 98, observed_at: now - 120, resets_at: now + 60 }];
  assert.equal(settingsRow(account).figure, '98%');
  const row = settingsRow(account, now + 60);
  assert.equal(row.slot, '– 3m ago');
  assert.equal(row.threshold, false);
  assert.equal(row.meters[0].reset, true);
  assert.equal(row.meters[0].dimmed, true);
  assert.equal(account.windows[0].reset, false);
});

test('S3: a usage read failure cannot make an unknown window look recently observed', () => {
  const row = popoverModel({ usage: snapshot(), error: 'Read failed', fetched_at: now }, now).rows[0];
  assert.equal(row.slot, '– 20d ago');
  assert.match(row.tooltip, /last seen 20d ago/);
});

for (const state of ['missing', 'replaced', 'off', 'unset', 'unsupported', 'no_limits', 'future-state', 'live', undefined]) {
  test(`S3: statusline_usage ${state ?? 'absent'} supplies its context without changing readiness`, () => {
    const account = { ...rawAccount(), ...(state === undefined ? {} : { statusline_usage: state }) };
    const expected = state === 'no_limits' ? 'No usage limits on this login · Updated 20d ago'
      : state === undefined || state === 'live' ? 'Updated 20d ago'
      : "Usage updates only from agt's jobs · Updated 20d ago";
    for (const row of [settingsRow(account), popoverModel({ usage: snapshot(account) }, now).rows[0]]) {
      assert.equal(row.detailContext, expected);
      assert.equal(row.label, 'Available');
      assert.equal(row.action, null);
    }
    assert.equal(settingsRow(account).usageSetupCommand, ['missing', 'replaced'].includes(state) ? 'agt setup' : '');
    assert.equal(popoverModel({ usage: snapshot(account) }, now).rows[0].usageSetupCommand, '');
  });
}
