import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { contentHeight, createHeightReporter, cssPanelMotion } from './height.js';

// Read the actual tokens so the test adds no second owner of motion literals.
const css = readFileSync(new URL('../motion.css', import.meta.url), 'utf8');
const styles = { getPropertyValue: (name) => css.match(new RegExp(`${name}:\\s*([^;]+);`))[1] };
const tokens = cssPanelMotion(styles);
function fixture() {
  const frames = [], reports = [];
  let height = 68, reduced = false;
  const reporter = createHeightReporter({ measure: () => height, send: (value) => reports.push(value),
    schedule: (callback) => frames.push(callback), reducedMotion: () => reduced, motion: () => tokens,
    onError: (error) => { throw error; } });
  return { reporter, reports, frames, flush: () => frames.shift()?.(),
    height: (value) => { height = value; }, reduced: (value) => { reduced = value; } };
}
test('first show and loading to first answer snap; subsequent heights use CSS motion', () => {
  const f = fixture();
  f.reporter.show(); f.flush();
  assert.equal(f.reports[0].durationMs, 0);
  f.height(224); f.reporter.state({ loading: false }); f.flush();
  assert.equal(f.reports[1].durationMs, 0);
  f.height(252); f.reporter.state({ loading: false }); f.flush();
  assert.equal(f.reports[2].durationMs, tokens.durationMs);
  assert.deepEqual(f.reports[2].curve, tokens.curve);
  f.height(280); f.reporter.show(); f.flush();
  assert.equal(f.reports[3].durationMs, tokens.durationMs);
});
test('first answer before the first show still snaps on first show', () => {
  const f = fixture();
  f.reporter.state({ loading: false }); f.flush();
  f.reporter.show(); f.flush();
  assert.equal(f.reports.length, 2);
  assert.ok(f.reports.every((value) => value.durationMs === 0));
});
test('observations coalesce and retain the loading transition snap', () => {
  const f = fixture();
  f.reporter.request(); f.flush();
  f.reporter.state({ loading: false }); f.reporter.state({ loading: false });
  f.height(223.2); f.reporter.request(); f.reporter.request();
  assert.equal(f.frames.length, 1); f.flush();
  assert.equal(f.reports[1].height, 224);
  assert.equal(f.reports[1].durationMs, 0);
  f.reporter.request(); f.flush();
  assert.equal(f.reports.length, 2, 'viewport resize feedback cannot cause another IPC');
});
test('Reduce Motion snaps every height change', () => {
  const f = fixture();
  f.reporter.state({ loading: false }); f.flush();
  f.reduced(true); f.height(300); f.reporter.request(); f.flush();
  assert.equal(f.reports.at(-1).durationMs, 0);
  f.reduced(false); f.height(328); f.reporter.request(); f.flush();
  assert.equal(f.reports.at(-1).durationMs, tokens.durationMs);
});
test('intrinsic content includes pinned rows and margins, excludes hidden rows and the scroll cap', () => {
  const body = {}, header = { offsetHeight: 26 }, accounts = { offsetHeight: 842 },
    empty = { hidden: true, offsetHeight: 28 }, footer = { offsetHeight: 26 };
  const style = (part) => part === body ? { paddingTop: '5px', paddingBottom: '7px' }
    : part === header ? { marginBottom: '2px' } : part === footer ? { marginTop: '4px' } : {};
  assert.equal(contentHeight(body, [header, accounts, empty, footer], style), 912);
  accounts.hidden = true; empty.hidden = false; footer.hidden = true;
  assert.equal(contentHeight(body, [header, accounts, empty, footer], style), 68);
});
