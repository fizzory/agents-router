import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { createRow, updateRow } from '../row-view.js';
import { popoverModel } from './rows.js';
// A small DOM seam exercises the actual renderer with no browser dependency.
class Node {
  constructor(document, tag) {
    this.ownerDocument = document; this.tagName = tag; this.children = []; this.attributes = {};
    this.dataset = {}; this.style = {}; this.hidden = false; this.tabIndex = tag === 'button' ? 0 : -1;
    this.classes = new Set(); this.events = {};
    this.classList = { toggle: (name, on) => on ? this.classes.add(name) : this.classes.delete(name),
      add: (name) => this.classes.add(name), remove: (name) => this.classes.delete(name) };
  }
  set textContent(text) { this.text = text; this.children = []; }
  get textContent() { return (this.text || '') + this.children.map((child) => child.textContent).join(''); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.text = ''; this.children = nodes; }
  setAttribute(name, value) { this.attributes[name] = value; }
  getAttribute(name) { return this.attributes[name]; }
  addEventListener(name, action) { this.events[name] = action; }
}
const document = { createElement: (tag) => new Node(document, tag), createTextNode: (text) => { const node = new Node(document, '#text'); node.textContent = text; return node; } };
const data = JSON.parse(readFileSync(new URL('../../../tests/fixtures/app/usage-popover.json', import.meta.url)));
const rows = popoverModel({ usage: data }, data.generated_at, { timeZone: 'UTC' }).rows;
const buttons = (node, hidden = false) => {
  hidden ||= node.hidden;
  return [node.tagName === 'button' && !hidden && node.tabIndex >= 0 ? node : null,
    ...node.children.flatMap((child) => buttons(child, hidden))].filter(Boolean);
};
test('every S1/S2 fixture row renders exactly one tab stop, full accessible text, and only IPC callbacks', () => {
  for (const model of rows) {
    const calls = [];
    const view = createRow(document, (row) => calls.push(['settings', row.key]), (row) => calls.push([row.action, row.key]));
    updateRow(view, model);
    assert.equal(buttons(view.row).length, 1, model.key);
    const focus = buttons(view.row)[0];
    assert.ok(focus.getAttribute('aria-label').includes(model.tooltip), model.key);
    assert.equal(view.provider.textContent, model.providerFirst ? model.provider : '');
    assert.equal(view.start.textContent + view.end.textContent, model.name);
    assert.equal(view.number.textContent, model.figure);
    assert.equal(view.action.textContent, model.button);
    assert.equal(view.state.textContent, model.state);
    focus.events.click();
    assert.deepEqual(calls, [[model.action || 'settings', model.key]]);
    if (model.action) { view.link.events.click(); assert.deepEqual(calls[1], ['settings', model.key]); }
  }
});
test('meter motion is gated on fresh data; status/open render snaps and retains the same focusable node', () => {
  const model = rows[0], view = createRow(document, () => {}, () => {});
  updateRow(view, model);
  const link = view.link;
  assert.equal(view.row.classes.has('is-fresh'), false);
  assert.equal(view.tracks[1].fill.style.transform, 'translateX(-14%)');
  const updated = { ...model, figure: '96%', meters: model.meters.map((meter) => ({ ...meter, percent: 96 })) };
  updateRow(view, updated, true);
  assert.equal(view.row.classes.has('is-fresh'), true);
  assert.ok(view.number.children.length > 0);
  updateRow(view, { ...updated, figure: '97%' }, false);
  assert.equal(view.row.classes.has('is-fresh'), false);
  assert.equal(view.number.textContent, '97%');
  assert.equal(view.link, link);
  updateRow(view, model, true, true);
  assert.equal(view.number.textContent, '86%');
});
