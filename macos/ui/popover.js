import * as api from './tauri.js';
import { popoverModel } from './logic/rows.js';
import { createRow, updateRow, fitName, element } from './row-view.js';
import { contentHeight, createHeightReporter, cssPanelMotion } from './logic/height.js';
const $ = (selector) => document.querySelector(selector);
const accounts = $('#accounts'), status = $('#status'), footer = $('#last-row');
const refresh = $('#refresh'), refreshIcon = refresh.querySelector('svg');
const menu = $('#app-menu'), more = $('#more'), tooltip = $('#tooltip');
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const views = new Map();
let generation = -1, freshGeneration = -1, snapshot = null, reading = false, footerKey = '';
const motion = (name) => parseFloat(getComputedStyle(document.documentElement).getPropertyValue(name));
const content = [$('.popover-head'), accounts, $('#empty'), $('#reading'), footer];
const heightReporter = createHeightReporter({
  measure: () => contentHeight(document.body, content, (part) => getComputedStyle(part)),
  send: api.setPopoverHeight,
  // Retained NSPanel pages can suspend animation frames while hidden. Size
  // reports must also run before the first show, so coalesce in a microtask.
  schedule: (callback) => queueMicrotask(callback),
  reducedMotion: () => reduced.matches,
  motion: () => cssPanelMotion(getComputedStyle(document.documentElement)),
  onError: report,
});
function report(error) {
  const message = String(error);
  status.textContent = message;
  renderFooter({ kind: 'problem', text: message, button: 'Retry', action: 'refresh', tooltip: message });
}
function settings(model) { api.openSettings({ runtime: model.runtime, account: model.name }).catch(report); }
async function action(model, button) {
  if (button.getAttribute('aria-busy') === 'true') return;
  button.setAttribute('aria-busy', 'true');
  button.setAttribute('aria-disabled', 'true');
  let timer;
  const progress = () => {
    const icon = refreshIcon.cloneNode(true); icon.classList.remove('turning');
    button.replaceChildren(icon, document.createTextNode(model.button));
    icon.addEventListener('animationend', () => {
      icon.classList.remove('turning');
      if (button.getAttribute('aria-busy') === 'true') requestAnimationFrame(() => startTurn(icon));
    });
    startTurn(icon);
  };
  if (model.action === 'check') progress();
  else timer = setTimeout(progress, motion('--terminal-delay'));
  try {
    await (model.action === 'login' ? api.loginAccount(model.runtime, model.name) : api.checkAccount(model.runtime, model.name));
  } catch (error) { report(error); }
  finally { clearTimeout(timer); button.setAttribute('aria-busy', 'false'); button.removeAttribute('aria-disabled'); button.textContent = model.button; }
}
function renderFooter(model) {
  const key = JSON.stringify(model);
  if (key === footerKey) return;
  footerKey = key; footer.hidden = !model; footer.replaceChildren();
  if (!model) return;
  footer.dataset.tooltip = model.tooltip;
  const activate = () => (model.action === 'update' ? api.checkForUpdates() : model.action === 'refresh' ? api.refreshUsage() : api.openSettings()).catch(report);
  if (model.button) {
    const button = element(document, 'button', 'inline-action', model.button);
    button.setAttribute('aria-label', `${model.text}. ${model.button}`);
    button.addEventListener('click', activate);
    footer.append(element(document, 'span', 'message', model.text), button);
  } else {
    const button = element(document, 'button', 'update-action', model.text);
    button.addEventListener('click', activate); footer.append(button);
  }
}
function render(next, isFresh = false) {
  if (next.generation < generation) return;
  const animate = isFresh && next.generation > freshGeneration && document.body.dataset.active === 'true';
  if (isFresh) freshGeneration = Math.max(freshGeneration, next.generation);
  generation = next.generation; snapshot = next;
  let model;
  try { model = popoverModel(next, Date.now() / 1000); } catch (error) { report(error); return; }
  $('#heading').textContent = model.header;
  reading = model.reading; refresh.setAttribute('aria-busy', String(reading));
  refresh.setAttribute('aria-label', reading ? 'Refreshing usage' : 'Refresh usage');
  refresh.dataset.tooltip = reading ? 'Refreshing usage' : 'Refresh usage';
  refresh.classList.toggle('refresh-busy', reading);
  if (reading) startTurn(refreshIcon);
  $('#empty').hidden = !model.empty;
  $('#reading').hidden = !model.first;
  accounts.hidden = !model.rows.length;
  status.textContent = next.error || (reading ? 'Reading agt…' : '');
  const positions = new Map([...views].map(([key, view]) => [key, view.row.getBoundingClientRect().top]));
  const keys = new Set(model.rows.map((row) => row.key));
  for (const [key, view] of views) if (!keys.has(key)) {
    views.delete(key);
    // Exiting rows are never tabbable, even during the short fade.
    view.link.tabIndex = view.action.tabIndex = -1;
    if (animate && !reduced.matches) {
      view.row.style.top = `${view.row.offsetTop}px`;
      view.row.style.width = `${view.row.clientWidth}px`;
      view.row.setAttribute('aria-hidden', 'true');
      view.row.classList.add('row-leave');
      view.row.addEventListener('animationend', () => view.row.remove(), { once: true });
    } else view.row.remove();
  }
  model.rows.forEach((row, index) => {
    let view = views.get(row.key);
    if (!view) {
      view = createRow(document, settings, action); views.set(row.key, view);
      if (animate) view.row.classList.add('row-enter');
    }
    updateRow(view, row, animate, reduced.matches);
    // Do not reinsert retained rows on status updates: preserve keyboard focus.
    if (accounts.children[index] !== view.row) accounts.insertBefore(view.row, accounts.children[index] || null);
    fitName(view);
  });
  if (animate && !reduced.matches) for (const [key, view] of views) {
    const oldTop = positions.get(key), newTop = view.row.getBoundingClientRect().top;
    if (oldTop === undefined || oldTop === newTop) continue;
    view.row.classList.remove('row-moving'); view.row.style.transform = `translateY(${oldTop - newTop}px)`;
    requestAnimationFrame(() => { view.row.classList.add('row-moving'); view.row.style.transform = ''; });
  }
  renderFooter(model.footer);
  heightReporter.state({ loading: model.first });
  if (reduced.matches && document.body.dataset.active === 'true') {
    document.body.classList.remove('content-change'); void document.body.offsetWidth;
    document.body.classList.add('content-change');
  }
}
function startTurn(icon) {
  if (reduced.matches || document.body.dataset.active !== 'true' || icon.classList.contains('turning')) return;
  icon.classList.add('turning');
}
refreshIcon.addEventListener('animationend', () => {
  refreshIcon.classList.remove('turning');
  if (reading) requestAnimationFrame(() => startTurn(refreshIcon));
});
refresh.addEventListener('click', () => { if (!reading) api.refreshUsage().catch(report); });
$('#settings').addEventListener('click', () => api.openSettings().catch(report));
// The Accounts sheet and runtime installation are owned by later plan steps.
$('#add').addEventListener('click', () => api.openSettings({ account: '' }).catch(report));
$('#check-updates').addEventListener('click', () => { closeMenu(); api.checkForUpdates().catch(report); });
$('#quit').addEventListener('click', () => api.quitApp().catch(report));
function closeMenu(focus = false) { menu.hidden = true; more.setAttribute('aria-expanded', 'false'); if (focus) more.focus(); }
more.addEventListener('click', () => {
  if (!menu.hidden) { closeMenu(true); return; }
  hideTooltip(); menu.hidden = false; more.setAttribute('aria-expanded', 'true'); menu.querySelector('button').focus();
});
menu.addEventListener('keydown', (event) => {
  const items = [...menu.querySelectorAll('button')], index = items.indexOf(document.activeElement);
  const next = event.key === 'ArrowDown' ? (index + 1) % items.length : event.key === 'ArrowUp' ? (index + items.length - 1) % items.length
    : event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : items.findIndex((item) => event.key.length === 1 && item.textContent.toLowerCase().startsWith(event.key.toLowerCase()));
  if (next >= 0) { event.preventDefault(); items[next].focus(); }
  if (event.key === 'Tab') closeMenu();
});
document.addEventListener('pointerdown', (event) => { if (!menu.contains(event.target) && !more.contains(event.target)) closeMenu(); });
let tooltipTimer, tooltipTarget = null, neighbour = false;
function hideTooltip() {
  clearTimeout(tooltipTimer);
  if (tooltipTarget) tooltipTarget.removeAttribute('aria-describedby');
  tooltipTarget = null; tooltip.hidden = true; tooltip.classList.remove('visible');
}
function showTooltip(target, immediate) {
  const warm = neighbour; hideTooltip(); tooltipTarget = target;
  const show = () => {
    tooltip.textContent = target.dataset.tooltip; tooltip.hidden = false;
    const box = target.getBoundingClientRect(), size = tooltip.getBoundingClientRect();
    tooltip.style.left = `${Math.max(6, Math.min(box.left, innerWidth - size.width - 6))}px`;
    tooltip.style.top = `${box.top >= size.height + 6 ? box.top - size.height - 5 : Math.min(box.bottom + 5, innerHeight - size.height - 6)}px`;
    target.setAttribute('aria-describedby', 'tooltip');
    requestAnimationFrame(() => tooltip.classList.add('visible')); neighbour = true;
  };
  if (immediate || warm) show(); else tooltipTimer = setTimeout(show, motion('--tooltip-delay'));
}
document.addEventListener('pointerover', (event) => {
  const target = event.target.closest('[data-tooltip]');
  if (target && target !== tooltipTarget) showTooltip(target, false);
  else if (!target) { hideTooltip(); neighbour = false; }
});
document.addEventListener('focusin', (event) => { const target = event.target.closest('[data-tooltip]'); if (target) showTooltip(target, true); });
document.addEventListener('focusout', hideTooltip);
document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
  event.preventDefault();
  if (!menu.hidden) { closeMenu(true); return; }
  if (!tooltip.hidden) hideTooltip();
  api.hidePopover().catch(report);
});
function active(value) {
  document.body.dataset.active = String(value);
  if (value) {
    heightReporter.show();
    document.body.classList.remove('panel-show'); void document.body.offsetWidth; document.body.classList.add('panel-show');
    if (reading) startTurn(refreshIcon);
  } else {
    hideTooltip(); closeMenu(); refreshIcon.classList.remove('turning');
    for (const row of accounts.querySelectorAll('.row-leave')) row.remove();
    for (const view of views.values()) { view.number.textContent = view.model.figure; view.row.classList.remove('row-enter'); }
  }
}
window.addEventListener('focus', () => active(true));
window.addEventListener('blur', () => active(false));
document.addEventListener('visibilitychange', () => active(!document.hidden && document.hasFocus()));
const contentObserver = new ResizeObserver(() => {
  for (const view of views.values()) fitName(view);
  heightReporter.request();
});
for (const part of content) contentObserver.observe(part);
// Subscribe before the initial read. Only the successful fresh-data event animates meters.
await api.onUsageUpdated((value) => render(value, true));
await api.onPopoverStatus((value) => render(value, false));
await api.onUpdateRequested(() => { status.textContent = 'Updates aren’t configured yet.'; });
try { render(await api.getSnapshot()); } catch (error) { report(error); }
active(document.hasFocus());
