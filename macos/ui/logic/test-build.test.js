import test from 'node:test';
import assert from 'node:assert/strict';
import { truncateStart, testBuildRows, testBuildMatches } from './test-build.js';

const rows = [
  { key: 'app_support', path: '/Users/me/Library/Application Support/Agents Router Test', display: '~/Library/Application Support/Agents Router Test', source: 'default', origin: null },
  { key: 'bin_dir', path: '/tmp/agt-bin', display: '/tmp/agt-bin', source: 'flag', origin: '--test-bin-dir' },
  { key: 'agt_home', path: '/Users/me/Library/Application Support/Agents Router Test/agt-home', display: '~/Library/Application Support/Agents Router Test/agt-home', source: 'env', origin: 'AGT_HOME' },
  { key: 'config_dir', path: '/Users/me/Library/Application Support/Agents Router Test/agt-home', display: '~/Library/Application Support/Agents Router Test/agt-home', source: 'default', origin: null },
];

test('long paths keep their end, cut at a folder boundary', () => {
  assert.equal(truncateStart('~/Library/Application Support/Agents Router Test/agt-home'), '…/Agents Router Test/agt-home');
  assert.equal(truncateStart('/tmp/agt-bin'), '/tmp/agt-bin');
  assert.equal(truncateStart('/a/' + 'x'.repeat(60), 20), '…' + 'x'.repeat(19));
  assert.ok(truncateStart('/' + 'a/'.repeat(40) + 'end').length <= 40);
});

test('rows carry labels, a chip only when overridden, and the copy label', () => {
  const view = testBuildRows(rows);
  assert.deepEqual(view.map(r => r.label), ['App support', 'Bin folder', 'agt home', 'Settings folder']);
  assert.deepEqual(view.map(r => r.chip), [null, 'Flag', 'Env', null]);
  assert.equal(view[2].value, '…/Agents Router Test/agt-home');
  assert.equal(view[2].copyLabel, 'Copy agt home: /Users/me/Library/Application Support/Agents Router Test/agt-home (AGT_HOME)');
  assert.equal(view[1].copyLabel, 'Copy Bin folder: /tmp/agt-bin (--test-bin-dir)');
  assert.equal(view[0].copyLabel, 'Copy App support: /Users/me/Library/Application Support/Agents Router Test');
  assert.equal(view[2].path, rows[2].path);
});

test('the release and a failed invoke render nothing', () => {
  assert.deepEqual(testBuildRows(null), []);
  assert.deepEqual(testBuildRows(undefined), []);
  assert.equal(testBuildMatches([], 'test'), false);
});

test('search matches the block title, labels and paths', () => {
  const view = testBuildRows(rows);
  for (const query of ['Test build', 'agt home', 'agt-bin', 'settings folder', 'flag']) assert.ok(testBuildMatches(view, query), query);
  assert.equal(testBuildMatches(view, 'max jobs'), false);
  assert.equal(testBuildMatches(view, '  '), false);
});
