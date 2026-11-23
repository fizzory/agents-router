import assert from 'node:assert/strict';
import test from 'node:test';
import { skeletonRowCount } from './skeleton.js';

test('skeleton rows default to three for missing or non-numeric values', () => {
  for (const value of [null, undefined, '', '  ', 'garbage', 'NaN', 'Infinity', NaN, Infinity, -Infinity, true, {}]) {
    assert.equal(skeletonRowCount(value), 3, String(value));
  }
});

test('skeleton rows use a whole remembered count clamped to one through six', () => {
  for (const [value, expected] of [[0, 1], ['0', 1], [-2, 1], [1, 1], ['5', 5], [6, 6], [9, 6], ['9', 6], ['2.9', 2]]) {
    assert.equal(skeletonRowCount(value), expected, String(value));
  }
});
