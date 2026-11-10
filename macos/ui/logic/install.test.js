import test from 'node:test';
import assert from 'node:assert/strict';
import { installDialog, dialogKey } from './install.js';
test('replacement names both versions and Keep never replaces', () => {
  const model = installDialog({ dialog: 'replace', version: '0.17', foreign: { version: '0.16', source: 'uv' } });
  assert.equal(model.title, 'Replace agt 0.16 (uv) with 0.17?');
  assert.deepEqual(model.buttons, [['Keep', 'keep'], ['Replace', 'replace']]);
  assert.equal(dialogKey(model, 'Escape'), 'keep'); assert.equal(dialogKey(model, 'Enter'), 'replace');
});
test('downgrade defaults to Cancel; progress has no keyboard action', () => {
  assert.equal(dialogKey(installDialog({ dialog: 'downgrade' }), 'Enter'), 'cancel');
  assert.equal(dialogKey(installDialog({ dialog: 'progress' }), 'Enter'), undefined);
  assert.equal(dialogKey(installDialog({ dialog: 'progress' }), 'Escape'), undefined);
});
test('ready names Terminal setup and Later; no dialog when done', () => {
  const model = installDialog({ dialog: 'ready' }); assert.equal(model.title, 'agt is ready.');
  assert.equal(dialogKey(model, 'Enter'), 'setup'); assert.equal(dialogKey(model, 'Escape'), 'later');
  assert.equal(installDialog({}), null);
});
test('move copies/relaunches; failures preserve space and recovery detail', () => {
  const move = installDialog({ dialog: 'move' }); assert.equal(move.default, 'move'); assert.equal(move.cancel, 'quit');
  const error = installDialog({ dialog: 'error', message: "Couldn't install agt. Free at least 12345 bytes, then try again." });
  assert.equal(error.title, "Couldn't install agt."); assert.match(error.detail, /12345 bytes/); assert.equal(error.default, 'retry');
  const upgrade = installDialog({ dialog: 'error', message: "Install didn't finish. Your previous agt still works. Copy interrupted." });
  assert.match(upgrade.title, /previous agt still works/); assert.equal(upgrade.detail, 'Copy interrupted.');
});
