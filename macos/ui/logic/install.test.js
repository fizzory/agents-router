import test from 'node:test';
import assert from 'node:assert/strict';
import { installDialog, dialogKey } from './install.js';
test('replacement names both versions and Keep never replaces', () => {
  const model = installDialog({ dialog: 'replace', version: '0.17', foreign: { version: '0.16', source: 'uv' } });
  assert.equal(model.title, 'Replace agt 0.16 from uv?');
  assert.equal(model.detail, 'Agents Router installs agt 0.17. Restore previous agt in General switches back.');
  assert.deepEqual(model.buttons, [['Keep', 'keep'], ['Replace', 'replace']]);
  assert.equal(dialogKey(model, 'Escape'), 'keep'); assert.equal(dialogKey(model, 'Enter'), 'replace');
});
test('downgrade defaults to Cancel; progress has no keyboard action', () => {
  const model = installDialog({ dialog: 'downgrade', version: '0.18.0', bundle_version: '0.17.0' });
  assert.equal(model.title, 'Install an older agt?');
  assert.equal(model.detail, 'agt 0.18.0 is installed. This app has 0.17.0.');
  assert.equal(dialogKey(model, 'Enter'), 'cancel');
  assert.equal(installDialog({ dialog: 'progress' }).title, 'Installing agt…');
  assert.equal(installDialog({ dialog: 'progress', message: 'Checking setup…' }).title, 'Checking setup…');
  assert.equal(dialogKey(installDialog({ dialog: 'progress' }), 'Enter'), undefined);
  assert.equal(dialogKey(installDialog({ dialog: 'progress' }), 'Escape'), undefined);
});
test('ready sets up in Terminal or Later; no dialog when done', () => {
  const model = installDialog({ dialog: 'ready' }); assert.equal(model.title, 'agt is installed.');
  assert.equal(model.detail, 'Finish setup to connect your CLIs.');
  assert.deepEqual(model.buttons, [['Later', 'later'], ['Set up in Terminal', 'setup']]);
  assert.equal(dialogKey(model, 'Enter'), 'setup'); assert.equal(dialogKey(model, 'Escape'), 'later');
  assert.equal(installDialog({}), null);
});
test('uninstall defaults to Cancel; Uninstall needs a deliberate click', () => {
  const model = installDialog({ dialog: 'uninstall' });
  assert.equal(model.title, 'Uninstall agt?');
  assert.equal(model.detail, 'Removes the agt this app installed. Your settings stay.');
  assert.deepEqual(model.buttons, [['Cancel', 'cancel'], ['Uninstall', 'confirm-uninstall']]);
  assert.equal(dialogKey(model, 'Enter'), 'cancel'); assert.equal(dialogKey(model, 'Escape'), 'cancel');
});
test('move copies/relaunches; failures preserve space and recovery detail', () => {
  const move = installDialog({ dialog: 'move' }); assert.equal(move.default, 'move'); assert.equal(move.cancel, 'quit');
  assert.equal(move.title, 'Move Agents Router to Applications?');
  const error = installDialog({ dialog: 'error', message: "Couldn't install agt. Free at least 12345 bytes, then try again." });
  assert.equal(error.title, "Couldn't install agt."); assert.match(error.detail, /12345 bytes/); assert.equal(error.default, 'retry');
  assert.deepEqual(error.buttons, [['Close', 'close'], ['Retry', 'retry']]);
  const upgrade = installDialog({ dialog: 'error', message: "Install didn't finish. Your previous agt still works. Copy interrupted." });
  assert.match(upgrade.title, /previous agt still works/); assert.equal(upgrade.detail, 'Copy interrupted.');
});
