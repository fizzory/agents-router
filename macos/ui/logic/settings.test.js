import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { controlFor, rowState, fileTarget, brokenFile, installState, matches, generalRows, fieldCommit, isTextField, dispatchEdit, errorMessage } from './settings.js';
import { SettingsHistory } from './settings-history.js';
import { makeControl } from '../settings-controls.js';
const fixture = async name => JSON.parse(await readFile(new URL(`../../../tests/fixtures/app/${name}.json`, import.meta.url)));
const config = await fixture('config-show-levels');
const row = key => config.settings.find(row => row.key === key);

test('every inventory control kind has an implemented UI control', async () => {
  const inventory = JSON.parse(await readFile(new URL('../settings-inventory.json', import.meta.url)));
  for (const value of Object.values(inventory.settings)) if (value.startsWith('control:')) assert.ok(controlFor(value.slice(8)), value);
  assert.equal(controlFor('fixed'), 'file');
});
test('selected level shows its own value while chip gives winning value and file:line', () => {
  const state = rowState(row('max_depth'), 'workspace', config);
  assert.equal(state.value, 1);
  assert.equal(state.chip, 'Local wins');
  assert.match(state.chipTooltip, /0 — .*settings.local.toml:1$/);
  assert.equal(rowState(row('max_depth'), 'local', config).value, 0);
  assert.equal(rowState(row('max_depth'), 'user', config).value, null);
});
test('user-only row under Workspace shows the user value read-only and User only', () => {
  const state = rowState(row('claude.primary'), 'workspace', config);
  assert.equal(state.value, '~/.claude'); assert.equal(state.editable, false); assert.equal(state.chip, 'User only');
  assert.equal(state.reset, false);
  assert.equal(rowState(row('claude.accounts'), 'user', config).chip, 'Workspace only');
});
test('General keeps four top-level keys then provider blocks; search matches label, key and description', () => {
  assert.deepEqual(generalRows(config).slice(0, 4).map(row => row.key), ['max_jobs', 'max_depth', 'job_retention_days', 'subagents.prefer_roles']);
  assert.equal(generalRows(config)[4].key, 'claude.primary');
  for (const query of ['max depth', 'max_depth', 'How deep workers']) assert.ok(matches(row('max_depth'), query));
});
test('every File only row opens its own resolved file and line, read-only', () => {
  const codex = row('codex.max_jobs_by_room'), kiro = row('kiro.max_jobs_by_room');
  assert.deepEqual(fileTarget(codex, 'user'), { file: '<ROOT>/project/.agt/settings.toml', line: 3 });
  assert.deepEqual(fileTarget(kiro, 'workspace'), { file: '<ROOT>/agt/settings.toml', line: 13 });
  assert.equal(rowState(codex, 'workspace', config).editable, false);
});
test('broken settings file locks all rows and preserves agt message and line', async () => {
  const broken = await fixture('config-show-broken');
  const target = brokenFile(broken);
  assert.equal(target.message, broken.error); assert.equal(target.file, '<ROOT>/agt/settings.toml'); assert.equal(target.line, 2);
  assert.ok(broken.settings.every(row => !rowState(row, 'user', broken).editable));
});
test('uninstall is hidden for external agt; app-owned uninstall disables for blockers and enables at zero', async () => {
  const busy = { usage: await fixture('usage') };
  assert.equal(installState(busy).owned, true);
  assert.equal(installState(busy).uninstallDisabled, true); assert.equal(installState(busy).uninstallTooltip, '2 jobs running');
  busy.usage = await fixture('usage-idle'); assert.equal(installState(busy).uninstallDisabled, false);
  const external = structuredClone(busy.usage); external.install.source = 'uv';
  assert.equal(installState({ usage: external }).owned, false);
  assert.equal(installState({ usage: { ...busy.usage, install: undefined } }).owned, false);
  assert.deepEqual(installState({error:'agt not found'}).problem, {message:'agt not found',action:'Install'});
  assert.deepEqual(installState({error:'agt 0.16 is too old'}).problem, {message:'agt 0.16 is too old',action:'Repair'});
});
test('field commits one step on Enter then blur; typing stays in the focused field', () => {
  const commits = [], commit = fieldCommit('2', value => commits.push(value));
  assert.equal(commit('27'), true); assert.equal(commit('27'), false); assert.deepEqual(commits, ['27']);
  assert.ok(isTextField({ matches: selector => selector.includes('textarea') }));
  assert.equal(isTextField(null), false);
  const edits = [];
  dispatchEdit('undo', {activeElement:{matches:()=>true},editField:action=>edits.push(action),history:{undo:()=>assert.fail('field undo reached settings history')}});
  assert.deepEqual(edits,['undo']);
});
function harness(initialValue = null) {
  const calls = [], navigation = [], messages = [];
  let revision = 'r0', value = initialValue, active = 0, peak = 0;
  const mutate = async (args, remove) => {
    calls.push({ ...args, remove }); active++; peak = Math.max(peak, active); await Promise.resolve(); active--;
    if (args.revision !== revision) return { ok: false, error: { message: 'settings.toml changed since it was read' } };
    const previous = {level:args.level, present:value !== null, value}; value = remove ? null : args.value;
    revision = 'r' + calls.length;
    return {ok:true,changed:true,key:args.key,file:'/settings.toml',revision,previous};
  };
  const history = new SettingsHistory({ set: args => mutate(args, false), unset: args => mutate(args, true), navigate: target => navigation.push(target), reload: async () => {}, report:(key,message) => messages.push({key,message}) });
  return { history, calls, navigation, messages, changeExternally: () => { revision = 'external'; }, peak: () => peak };
}
test('undo and redo run exactly one request per step from previous at the original level; serialize', async () => {
  const h = harness(), target = {pane:'general',workspace:'/project',level:'workspace'};
  await h.history.commit({key:'max_depth',value:'2',level:'workspace',workspace:'/project',revision:'r0'},target);
  await h.history.commit({key:'max_depth',value:'3',level:'workspace',workspace:'/project',revision:'r1'},target);
  await Promise.all([h.history.undo(), h.history.undo()]);
  assert.equal(h.calls.length,4); assert.equal(h.calls[2].value,'2'); assert.equal(h.calls[3].remove,true);
  assert.equal(h.calls[2].revision,'r2'); assert.equal(h.calls[3].revision,'r3'); assert.equal(h.peak(),1);
  assert.deepEqual(h.navigation[0],{pane:'general',level:'workspace',workspace:'/project',key:'max_depth'});
  await h.history.redo(); assert.equal(h.calls.length,5); assert.equal(h.calls[4].value,'2');
  await h.history.redo(); assert.equal(h.calls.length,6); assert.equal(h.calls[5].value,'3');
});
test('undo switches pane and scope before mutation; changed revision refuses without losing history', async () => {
  const h = harness();
  await h.history.commit({key:'max_jobs',value:'4',level:'local',revision:'r0'}, {pane:'general',workspace:'/project',level:'local'});
  h.changeExternally(); const result = await h.history.undo();
  assert.equal(result.ok,false); assert.equal(h.calls.length,2); assert.equal(h.calls[1].revision,'r1');
  assert.deepEqual(h.navigation[0],{pane:'general',level:'local',workspace:'/project',key:'max_jobs'});
  assert.equal(h.messages[0].message,'Changed elsewhere. Showing current value.'); assert.equal(h.history.undoSteps.length,1);
});
test('undo restores an explicit empty names list and redo restores the edited names', async () => {
  const h = harness([]), target = {pane:'general',workspace:null,level:'user'};
  await h.history.commit({key:'claude.primary_models',value:'opus,sonnet',level:'user',revision:'r0'},target);
  await h.history.undo();
  assert.equal(h.calls[1].remove,false);
  assert.equal(h.calls[1].value,'[]');
  assert.equal(h.calls[1].revision,'r1');
  await h.history.redo();
  assert.equal(h.calls[2].value,'opus,sonnet');
  assert.equal(h.calls[2].revision,'r2');
});
test('a later deliberate edit cannot authorize undo across an outside revision change', async () => {
  const h=harness(), target={pane:'general',workspace:null,level:'user'};
  await h.history.commit({key:'max_jobs',value:'4',level:'user',revision:'r0'},target);
  h.changeExternally();
  await h.history.commit({key:'max_depth',value:'1',level:'user',revision:'external'},target);
  await h.history.undo();
  const result=await h.history.undo();
  assert.equal(result.ok,false);
  assert.equal(h.calls.at(-1).revision,'r1');
  assert.equal(h.messages.at(-1).message,'Changed elsewhere. Showing current value.');
});
test('exit-3 refusal displays agt own ask without retry or a history step', async () => {
  const refusal = await fixture('config-set-needs-user-words');
  const calls=[], messages=[];
  const history = new SettingsHistory({set: async args => {calls.push(args);return {ok:false,exit_code:3,error:refusal};},unset:()=>assert.fail(),navigate:()=>{},reload:()=>{},report:(key,message)=>messages.push(message)});
  await history.commit({key:'max_depth'},{});
  assert.equal(messages[0],refusal.ask); assert.equal(calls.length,1); assert.equal(history.undoSteps.length,0);
  assert.equal(errorMessage({error:{message:'invalid value'}}),'invalid value');
});

// Small DOM double runs the actual control builder (not a duplicate renderer).
class Node {
  constructor(tag) { this.tagName=tag; this.children=[]; this.attributes={}; this.events={}; }
  setAttribute(k,v) { this.attributes[k]=String(v); }
  append(...nodes) { this.children.push(...nodes); }
  addEventListener(k,v) { const existing=this.events[k]; this.events[k]=event=>{existing?.(event);v(event);}; }
  blur() { this.events.blur?.(); }
  dataset={};
}
test('control kinds render actual spinbutton, switch, choice, fields and file output', () => {
  globalThis.document={createElement:tag=>new Node(tag)};
  for (const kind of ['int','float','bool','choice','text','names','auto_copy','fixed']) {
    const setting={...row('max_depth'),kind,choices:['one','two']};
    const control=makeControl(setting,{value:kind==='bool'?true:2,label:'Max depth',editable:true},()=>{});
    assert.ok(control);
    if (kind==='int'||kind==='float') assert.equal(control.children[0].attributes.role,'spinbutton');
    if (kind==='bool') assert.equal(control.attributes.role,'switch');
    if (kind==='choice') assert.equal(control.attributes['aria-haspopup'],'menu');
    if (kind==='fixed') assert.equal(control.tagName,'output');
  }
  delete globalThis.document;
});
test('actual stepper clamps arrows and commits one step on Enter plus blur', () => {
  globalThis.document={createElement:tag=>new Node(tag)};
  const saves = [];
  const control=makeControl(row('max_jobs'),{value:1,label:'Max jobs',editable:true},value=>saves.push(value));
  const input=control.children[0];
  input.events.keydown({key:'ArrowDown',preventDefault(){}});
  assert.equal(input.value,'1');
  input.events.keydown({key:'ArrowUp',preventDefault(){}});
  input.events.keydown({key:'ArrowUp',preventDefault(){}});
  input.events.keydown({key:'Enter'});
  assert.deepEqual(saves,['3']);
  delete globalThis.document;
});
test('environment provenance has a named chip without a nonexistent scope target', () => {
  const state=rowState(row('codex.primary'),'user',config);
  assert.equal(state.chip,'Environment wins');assert.equal(state.chipLevel,null);assert.match(state.chipTooltip,/CODEX_HOME/);
});
