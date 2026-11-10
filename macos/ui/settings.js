import { configShow, configSet, configUnset, getSettingsTarget, onSettingsTarget, onSettingsEdit,
  getOpenAtLogin, setOpenAtLogin, getSnapshot, onUsageUpdated, onSettingsStatus, chooseWorkspace, openSettingsFile, showSettingMenu, onSettingsHistory } from './tauri.js';
import { LEVELS, PROVIDERS, rowState, labelFor, matches, paneFor, fileTarget, brokenFile, errorMessage, installState, isTextField, dispatchEdit } from './logic/settings.js';
import { SettingsHistory } from './logic/settings-history.js';
import { element, icon, makeControl, popup } from './settings-controls.js';
import { attachTooltips } from './tooltip.js';

const $ = selector => document.querySelector(selector);
const status = $('#status'), rows = $('#settings'), login = $('#open-at-login'), search = $('#search');
const scopes = [], cache = new Map(), errors = new Map();
let scope = { level: 'user', workspace: null }, config = null, generation = 0, busy = false, snapshot = {}, flashKey = null, pendingRead = false;
$('#search-icon').append(icon('search'));
const hideTooltip = attachTooltips($('#tooltip'));
const history = new SettingsHistory({ set: configSet, unset: configUnset,
  navigate: target => navigate(target), reload: () => load(true), report: showError });

function showError(key, message) {
  errors.set(key, message); render();
  $('#announcement').textContent = message;
  const row = [...document.querySelectorAll('.setting-row')].find(item => item.dataset.key === key);
  row?.scrollIntoView({ block: 'nearest' });
}
function announceFailure(error) { status.textContent = errorMessage(error); }
function run(action) { return Promise.resolve().then(action).catch(announceFailure); }
function setPane(pane) {
  document.body.dataset.pane = pane;
  const title = pane.charAt(0).toUpperCase() + pane.slice(1);
  document.title = title; $('#pane-title').textContent = title; $('#content').setAttribute('aria-label', `${title} settings`);
  document.querySelectorAll('nav button').forEach(button => {
    if (button.dataset.pane === pane) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
  });
}
async function navigate(target) {
  hideTooltip();
  setPane(target.pane || 'general');
  scope = { level: target.level || 'user', workspace: target.workspace || null };
  if (scope.workspace && !scopes.includes(scope.workspace)) scopes.push(scope.workspace);
  if (!target.preserveSearch) search.value = '';
  flashKey = target.key || null;
  // Cached scope changes render synchronously; a fresh read follows.
  const cached = cache.get(scope.workspace);
  config = cached || null; render();
  await load();
}
async function load(force = false) {
  if (!force && document.activeElement?.dataset.settingField) { pendingRead = true; return; }
  const current = ++generation, workspace = scope.workspace;
  if (!config) status.textContent = 'Reading settings…';
  try {
    const value = await configShow(workspace);
    cache.set(workspace, value);
    if (current !== generation || workspace !== scope.workspace) return;
    if (!force && document.activeElement?.dataset.settingField) { pendingRead = true; return; }
    config = value; status.textContent = ''; render();
  } catch (error) {
    if (current === generation) { announceFailure(error); if (force) render(); }
  }
}
async function save(row, value, remove = false) {
  if (busy) return;
  const state = rowState(row, scope.level, config);
  if (!state.editable) return;
  busy = true; errors.delete(row.key);
  const target = { pane: paneFor(row), workspace: scope.workspace, level: scope.level };
  const args = { key: row.key, value, level: scope.level, workspace: scope.workspace, revision: state.own.revision };
  // Lock every row while the single write is pending. No retries/coalescing.
  render();
  try { await history.commit(args, target, remove); }
  catch (error) { await load(true); showError(row.key, errorMessage(error)); }
  finally { busy = false; render(); }
}
function createRow(row, providerFirst) {
  const state = rowState(row, scope.level, config);
  state.editable = state.editable && !busy;
  const node = element('div', 'setting-row'); node.dataset.key = row.key; node.dataset.tooltip = state.tooltip;
  node.tabIndex = -1;
  node.setAttribute('aria-label', `${state.label}, ${state.value ?? 'Not set'}, ${row.source}`);
  node.addEventListener('contextmenu', event => { event.preventDefault(); run(() => showSettingMenu(row.key)); });
  const provider = PROVIDERS[row.key.split('.')[0]];
  if (!provider) node.classList.add('top-level');
  if (providerFirst && provider) node.classList.add('provider-start');
  if (provider) node.append(element('span', 'provider', providerFirst ? provider : ''));
  const label = element('label', 'setting-label', labelFor(row.key));
  const control = element('span', 'control');
  const field = makeControl(row, state, value => run(() => save(row, value)));
  const id = `setting-${row.key.replaceAll('.', '-')}`;
  const focusable = field.matches('input,button') ? field : field.querySelector('input,button');
  if (focusable) { focusable.id = id; label.htmlFor = id; focusable.dataset.tooltip = state.tooltip; }
  control.append(field);
  const slot = element('span', 'row-slot');
  if (row.kind === 'fixed') {
    const target = fileTarget(row, scope.level);
    const button = element('button', 'file-open', 'Open file'); button.type = 'button';
    button.setAttribute('aria-label', `Open ${labelFor(row.key)} in settings.toml at line ${target.line}`);
    button.dataset.tooltip = `${target.file}:${target.line}`;
    button.addEventListener('click', () => run(() => openSettingsFile(target.file, target.line))); slot.append(button);
  } else if (state.chip) {
    const button = element('button', 'source-chip', state.chip); button.type = 'button'; button.dataset.tooltip = state.chipTooltip;
    button.setAttribute('aria-label', `${state.label}, ${state.chip}. ${state.chipTooltip}${state.chipLevel ? `. Show ${LEVELS[state.chipLevel]}` : ''}`);
    if (state.chipLevel) button.addEventListener('click', () => run(() => navigate({ pane: paneFor(row), level: state.chipLevel, workspace: scope.workspace, key: row.key })));
    slot.append(button);
  } else if (state.reset) {
    const button = element('button', 'icon-button'); button.type = 'button'; button.append(icon('undo'));
    button.disabled = !state.editable; button.setAttribute('aria-label', `Reset ${state.label}`); button.dataset.tooltip = `Reset ${state.label}`;
    button.addEventListener('click', () => run(() => save(row, null, true))); slot.append(button);
  } else if (config.error) { const lock = element('span', 'locked'); lock.append(icon('lock')); slot.append(lock); }
  node.append(label, control, slot);
  if (errors.has(row.key)) {
    const error = element('div', 'inline-error', errors.get(row.key)); error.id = `${id}-error`; error.setAttribute('role', 'alert');
    node.append(error); focusable?.setAttribute('aria-invalid', 'true'); focusable?.setAttribute('aria-errormessage', error.id);
  }
  if (flashKey === row.key) node.classList.add('setting-flash');
  return node;
}
function renderInstall() {
  const model = installState(snapshot), visible = document.body.dataset.pane === 'general' && !search.value;
  const banner = $('#install-banner'); banner.hidden = !model.problem || !visible;
  $('#general-dot').hidden = !model.problem;
  if (model.problem) {
    banner.querySelector('span').textContent = model.problem.message;
    banner.querySelector('button').textContent = model.problem.action;
  }
  $('#version').textContent = model.version ? `agt ${model.version}` : model.problem?.action === 'Install' ? 'Not installed' : 'Unavailable';
  $('#uninstall-wrapper').hidden = !model.owned;
  $('#uninstall-wrapper').dataset.tooltip = model.uninstallTooltip;
  $('#uninstall').disabled = model.uninstallDisabled;
}
function render() {
  hideTooltip();
  const pane = document.body.dataset.pane, query = search.value.trim();
  const general = pane === 'general', searching = !!query;
  $('#scope').hidden = !['general', 'roles'].includes(pane);
  $('#scope').textContent = scope.level !== 'user' && scope.workspace ? `${scope.workspace.split('/').pop()} › ${LEVELS[scope.level]} ▾` : 'User ▾';
  $('#scope').dataset.tooltip = scope.workspace || 'User settings';
  $('#general-footer').hidden = !general || searching;
  $('#pending-pane').hidden = general || searching;
  $('#pending-pane').textContent = `${$('#pane-title').textContent} is not available yet.`;
  rows.replaceChildren(); $('#file-rows').replaceChildren();
  let total = 0, fileCount = 0, lastProvider = null;
  if (config) for (const row of config.settings) {
    const provider = row.key.split('.')[0];
    const visible = searching ? pane !== 'activity' && matches(row, query) : general && paneFor(row) === 'general';
    const node = createRow(row, visible && provider !== lastProvider);
    node.hidden = !visible;
    if (visible) { lastProvider = provider; total++; }
    if (row.kind === 'fixed') { $('#file-rows').append(node); if (visible) fileCount++; } else rows.append(node);
  }
  $('#file-only').hidden = fileCount === 0;
  $('#file-only summary').textContent = `File only (${fileCount})`;
  if (searching && fileCount) $('#file-only').open = true;
  $('#no-match').hidden = !searching || total > 0;
  const broken = config && brokenFile(config), banner = $('#broken-banner');
  banner.hidden = !broken;
  if (broken) {
    banner.querySelector('span').textContent = broken.message;
    banner.querySelector('button').onclick = () => run(() => openSettingsFile(broken.file, broken.line));
  }
  renderInstall();
  if (flashKey) {
    const row = [...document.querySelectorAll('.setting-row')].find(item => item.dataset.key === flashKey);
    if (row && !row.hidden) {
      if (row.closest('details')) row.closest('details').open = true;
      row.scrollIntoView({ block: 'nearest' }); row.focus({ preventScroll: true });
    }
    flashKey = null;
  }
}
$('#scope').addEventListener('click', () => {
  const options = [{ label: 'User', value: { level: 'user', workspace: null }, selected: scope.level === 'user' }];
  for (const workspace of scopes) for (const level of ['workspace', 'local']) options.push({ label: `${workspace} › ${LEVELS[level]}`, value: { level, workspace }, selected: scope.workspace === workspace && scope.level === level });
  options.push({ label: 'Choose Folder…', value: null });
  popup($('#scope'), options, value => run(async () => {
    if (!value) { const workspace = await chooseWorkspace(); if (!workspace) return; value = { level: 'workspace', workspace }; }
    await navigate({ pane: document.body.dataset.pane, ...value, preserveSearch: true });
  }));
});
document.querySelectorAll('nav button').forEach(button => button.addEventListener('click', () => { setPane(button.dataset.pane); render(); }));
search.addEventListener('input', render);
search.addEventListener('keydown', event => { if (event.key === 'Escape') { search.value = ''; render(); search.blur(); } });
document.addEventListener('focusout', event => {
  if (!event.target.dataset.settingField) return;
  queueMicrotask(() => {
    if (pendingRead && !busy && !document.activeElement?.dataset.settingField) { pendingRead = false; run(() => load()); }
  });
});
async function edit(action) {
  const field = isTextField(document.activeElement);
  if (field) { dispatchEdit(action, { activeElement: document.activeElement, editField: action => document.execCommand(action), history }); return; }
  if (busy) return;
  busy = true;
  try { await dispatchEdit(action, { activeElement: document.activeElement, editField: action => document.execCommand(action), history }); } catch (error) { announceFailure(error); }
  finally { busy = false; render(); }
}
document.addEventListener('keydown', event => {
  if (event.metaKey && event.key.toLowerCase() === 'f') { event.preventDefault(); search.focus(); search.select(); }
  if (event.metaKey && event.key.toLowerCase() === 'z' && !isTextField(document.activeElement)) { event.preventDefault(); run(() => edit(event.shiftKey ? 'redo' : 'undo')); }
});
login.addEventListener('change', () => run(async () => {
  const previous = !login.checked; login.disabled = true;
  try { login.checked = await setOpenAtLogin(login.checked); }
  catch (error) { login.checked = previous; announceFailure(error); }
  finally { login.disabled = false; }
}));
// Install/Repair and uninstall transactions are implemented in step 7. Keep
// these explicit actions at the IPC boundary; never execute a frontend command.
$('#install-banner button').addEventListener('click', () => { status.textContent = `${$('#install-banner button').textContent} is not available in this build.`; });
$('#uninstall').addEventListener('click', () => { status.textContent = 'Uninstall is not available in this build.'; });
await onSettingsEdit(({ action }) => run(() => edit(action)));
await onSettingsTarget(target => run(() => navigate(target)));
await onSettingsHistory(({ key }) => { setPane('activity'); document.body.dataset.historyKey = key; search.value = key; render(); });
await onUsageUpdated(value => { snapshot = value; renderInstall(); });
await onSettingsStatus(value => { snapshot = value; renderInstall(); if (!busy) run(() => load()); });
try { snapshot = await getSnapshot(); } catch (error) { announceFailure(error); }
try { login.checked = await getOpenAtLogin(); } catch (error) { announceFailure(error); login.disabled = true; }
await navigate(await getSettingsTarget());
