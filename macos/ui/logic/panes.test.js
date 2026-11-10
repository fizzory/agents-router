import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { orderedRole, roleChoices, roleSource, moveProvider, rolesUndo, accountModels, AccountNameCheck,
  sharingModel, shareCell, shareResult, filterActivity, activityModel } from './panes.js';
import { SettingsHistory } from './settings-history.js';
const fixture = name => JSON.parse(readFileSync(new URL(`../../../tests/fixtures/app/${name}.json`, import.meta.url)));
const roles = fixture('roles-show-panes').roles;

test('only edit and full roles have numbered draggable order; read roles cannot move', () => {
  for (const role of roles) {
    assert.equal(orderedRole(role), ['edit', 'full'].includes(role.access));
    if (role.access === 'read') assert.equal(moveProvider(role, 0, 1), null);
  }
  assert.ok(roles.some(r => r.access === 'full'));
});
test('Move up and Move down announce the provider, role and resulting position', () => {
  const builder = roles.find(r => r.name === 'builder');
  const up = moveProvider(builder, 1, 0), down = moveProvider(builder, 0, 1);
  assert.equal(up.announcement, 'claude:work moved up to position 1 of 2 in builder.');
  assert.equal(down.announcement, 'codex moved down to position 2 of 2 in builder.');
  assert.deepEqual(up.edit.providers, ['claude:work', 'codex']);
  assert.equal(moveProvider(builder, 0, -1), null);
});
test('chip menus offer only fixture-allowed models and efforts, with current choices marked', () => {
  const role = fixture('roles-show-discovered').roles[0];
  for (const provider of role.providers) {
    const choices = roleChoices(provider);
    assert.deepEqual(choices.models.map(c => c.value), provider.choices.models.filter(c => c.allowed).map(c => c.model));
    assert.deepEqual(choices.efforts.map(c => c.value), provider.choices.efforts);
    assert.ok(!choices.models.some(c => !provider.choices.models.find(m => m.model === c.value)?.allowed));
  }
});
test('Local wins is derived from the enabled key source rather than role level', () => {
  const reviewer = roles.find(r => r.name === 'reviewer');
  const chip = roleSource({ ...reviewer, level: 'user' }, 'user');
  assert.equal(chip.text, 'Local wins'); assert.equal(chip.key, 'enabled'); assert.equal(chip.value, false);
  assert.match(chip.file, /settings.local.toml$/); assert.equal(chip.line, 3);
});
test('roles undo uses only structured restore fields, never the human command', async () => {
  const report = fixture('roles-config'), calls = [];
  const target = { key: 'roles.reviewer', workspace: '/fixture', level: 'user' };
  const step = rolesUndo({ ...report, undo: 'do not execute this' }, target);
  assert.deepEqual(step.edit, { kind: 'restore', undo_from: report.undo_from, if_unchanged: report.if_unchanged });
  assert.equal(step.undo, undefined);
  const history = new SettingsHistory({ roles: async args => { calls.push(args); return report; }, navigate: async () => {}, reload: async () => {}, report: assert.fail });
  await history.commitRole({ edit: { kind: 'model', entry: 'claude:work', value: 'opus' }, level: 'user', workspace: '/fixture' }, target);
  await history.undo(); assert.deepEqual(calls[1], { edit: step.edit, level: 'user', workspace: '/fixture' });
  await history.redo(); assert.equal(calls[2].edit.kind, 'restore');
});
test('refused role undo keeps the stack and reports a guarded refusal', async () => {
  let refuse = false; const errors = [];
  const history = new SettingsHistory({ roles: async () => refuse ? { error: { message: 'Changed since save' } } : fixture('roles-config'), navigate: async () => {}, reload: async () => {}, report: (...e) => errors.push(e) });
  await history.commitRole({}, { key: 'roles.reviewer' }); refuse = true; await history.undo();
  assert.equal(history.undoSteps.length, 1); assert.equal(errors[0][1], 'Changed elsewhere. Showing current value.');
});
test('untogglable accounts get a focusable badge instead of a dead switch', () => {
  const models = accountModels(fixture('accounts-list-locked'), null, 1800000000);
  for (const row of models.filter(m => !m.account.can_toggle)) {
    assert.equal(row.toggle.kind, 'badge'); assert.equal(row.toggle.tabIndex, 0); assert.ok(row.toggle.reason);
    if (row.account.name === 'primary') { assert.equal(row.name, ''); assert.equal(row.toggle.text, 'primary'); }
  }
  assert.equal(models.find(m => m.account.name === 'claude-lab').toggle.text, 'discovered');
  assert.equal(models.find(m => m.account.name === 'work').toggle.kind, 'switch');
});
test('account names stay canonical; usage states take precedence over auth fallback', () => {
  const list = { runtimes: [{ runtime: 'claude', accounts: [{ name: 'claude-work', enabled: true, can_toggle: true, auth: { loggedIn: false } }] }] };
  const [model] = accountModels(list, fixture('usage-popover'), 1794337200);
  assert.equal(model.name, 'claude-work'); assert.equal(model.availability, 'ok'); assert.equal(model.figure, '86%');
});
test('Add account stays disabled until agt passes its current provider/name check', async () => {
  let finish; const gate = new AccountNameCheck(() => new Promise(resolve => { finish = resolve; }));
  const pending = gate.validate('claude', 'spare'); assert.equal(gate.state.valid, false); assert.equal(gate.state.checking, true);
  finish(fixture('accounts-add-check')); await pending; assert.equal(gate.state.valid, true);
  gate.invalidate(); assert.equal(gate.state.valid, false);
  const next = gate.validate('claude', 'work'); finish(fixture('accounts-add-check-error')); await next;
  assert.equal(gate.state.valid, false); assert.ok(gate.state.message);
});
test('stale name-check replies cannot enable Add after a rename/provider change or close', async () => {
  const callbacks = [];
  const gate = new AccountNameCheck(() => new Promise(resolve => callbacks.push(resolve)));
  const first = gate.validate('claude', 'spare'), second = gate.validate('codex', 'work');
  callbacks[0]({ ok: true }); assert.equal(await first, null); assert.equal(gate.state.valid, false);
  gate.invalidate(); callbacks[1]({ ok: true }); assert.equal(await second, null); assert.equal(gate.state.valid, false);
});
test('Sharing state row and Waiting count come from status attention, including new items', () => {
  const list = fixture('share-list-kinds');
  const off = sharingModel(list, fixture('share-status-off')); assert.equal(off.state, 'Sharing off'); assert.equal(off.enabled, false);
  const on = sharingModel(list, fixture('share-status-waiting')); assert.equal(on.state, null); assert.equal(on.waiting.length, 2); assert.ok(on.waiting.every(item => !item.command.includes('<label>')));
  assert.equal(on.waiting[0].command, "agt share pick waiting --in 'codex'");
});
test('per-CLI picks obey listing editability; noneditable cells offer one runnable Copy command', () => {
  const model = sharingModel(fixture('share-list-kinds'), fixture('share-status-waiting'));
  const example = model.items.find(i => i.id === 'agents/example');
  assert.equal(example.cells.find(c => c.label === 'claude').editable, false);
  assert.equal(example.cells.find(c => c.label === 'codex').editable, true);
  assert.match(example.cells[0].command, /^agt share pick 'agents\/example' --not-in 'claude'$/);
  const instructions = model.items.find(i => i.kind === 'instructions'); assert.match(instructions.cells[0].command, /^agt share adopt/);
  const wait = shareCell({ id: 'hooks/a', kind: 'hooks' }, 'codex', { state: 'wait', editable: true }); assert.equal(wait.included, false);
});
test('kinds add/remove result line reflects written, waiting and failed outcomes', () => {
  for (const name of ['share-kinds-add', 'share-kinds-remove']) {
    const report = fixture(name); if (name.endsWith('remove')) assert.equal(shareResult(report), 'MCP servers off');
    else assert.match(shareResult(report), new RegExp(`^${report.written?.length || 0} shared`));
  }
  assert.equal(shareResult({ written: [1, 2, 3, 4], waiting: [1], failed: [1] }), '4 shared · 1 stays · 1 failed');
});
test('Activity lines carry time, schema label, old → new, scope and attribution tooltip', () => {
  const entry = fixture('activity-log').entries.find(e => e.key === 'max_depth' && e.new === 2);
  const row = activityModel(entry, fixture('config-show').settings, { locale: 'en-GB', timeZone: 'UTC' });
  assert.equal(row.time, '08:00'); assert.equal(row.label, 'Max depth'); assert.equal(row.change, 'Not set → 2'); assert.equal(row.level, 'User');
  assert.match(row.tooltip, /max_depth\nby command/); assert.match(row.tooltip, /settings.toml/);
});
test('Activity distinguishes failed counts and refused markers and Search only filters its input events', () => {
  const entries = fixture('activity-log').entries;
  const failed = filterActivity(entries, 'failed 1'); assert.equal(failed.length, 1); assert.equal(failed[0].marker, 'failed 1');
  assert.equal(filterActivity(entries, 'refused')[0].marker, 'refused');
  assert.ok(filterActivity(entries, 'max_depth').every(row => row.entry.key === 'max_depth'));
  assert.deepEqual(filterActivity(entries, 'this string does not exist'), []);
  assert.equal(entries.length, fixture('activity-log').entries.length);
});

test('Activity preserves structured old/new values without object-to-string coercion', () => {
  const entry = fixture('activity-log').entries.find(e => e.key === 'share.folders');
  const model = activityModel(entry);
  assert.ok(!model.change.includes('[object Object]'));
  assert.match(model.change, /"cli":"claude"/);
});
