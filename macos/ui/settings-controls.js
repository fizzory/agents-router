import { controlFor, displayValue, fieldCommit, cliValue } from './logic/settings.js';

export const element = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
};
export function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  for (const [key, value] of Object.entries({ viewBox: '0 0 24 24', width: '14', height: '14', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.75', 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true' })) svg.setAttribute(key, value);
  const paths = { undo: ['M9 14 4 9l5-5', 'M4 9h10.5a5.5 5.5 0 0 1 0 11H11'], search: ['M21 21l-4.3-4.3', 'M19 11a8 8 0 1 1-16 0 8 8 0 0 1 16 0'], lock: ['M6 11h12v10H6z', 'M8 11V7a4 4 0 0 1 8 0v4'], chevron: ['m9 18 6-6-6-6'], alert: ['M12 3 2 21h20L12 3z', 'M12 9v4', 'M12 17h.01'], copy: ['M9 9h12v12H9z', 'M5 15H3V3h12v2'], refresh: ['M3 12a9 9 0 1 0 3-6.7', 'M3 3v6h6'] };
  for (const d of paths[name]) { const path = document.createElementNS(svg.namespaceURI, 'path'); path.setAttribute('d', d); svg.append(path); }
  return svg;
}

// Hand-built menu shared by scope and choice controls. Arrow navigation,
// Home/End, Escape and type-ahead work without requiring a pointer.
export function popup(button, options, select) {
  const menu = element('div', 'popup-menu');
  menu.setAttribute('role', 'menu');
  menu.setAttribute('aria-label', button.getAttribute('aria-label') || button.textContent);
  button.setAttribute('aria-haspopup', 'menu');
  button.setAttribute('aria-expanded', 'true');
  let current = options.findIndex(item => item.selected && !item.heading);
  if (current < 0) current = options.findIndex(item => !item.heading);
  let prefix = '';
  let lastKey = 0;
  const close = (focus = true) => {
    menu.remove(); button.setAttribute('aria-expanded', 'false');
    document.removeEventListener('pointerdown', outside, true);
    document.removeEventListener('keydown', escape, true);
    if (focus) button.focus();
  };
  const outside = event => { if (!menu.contains(event.target) && !button.contains(event.target)) close(false); };
  const escape = event => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); } };
  const items = options.map((option, i) => {
    if (option.heading) { const heading = element('div', 'popup-heading', option.label); menu.append(heading); return null; }
    const item = element('button', 'popup-item', `${option.selected ? '✓' : '　'} ${option.label}`);
    item.type = 'button'; item.setAttribute('role', 'menuitemradio'); item.setAttribute('aria-checked', String(!!option.selected));
    item.tabIndex = i === current ? 0 : -1;
    item.addEventListener('click', () => { close(); select(option.value); });
    menu.append(item); return item;
  });
  menu.addEventListener('keydown', event => {
    const key = event.key;
    if (key === 'Tab') { close(false); return; }
    if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(key)) {
      event.preventDefault();
      const available = items.map((item, i) => item ? i : -1).filter(i => i >= 0);
      const at = available.indexOf(current);
      current = key === 'Home' ? available[0] : key === 'End' ? available.at(-1) : available[(at + (key === 'ArrowDown' ? 1 : -1) + available.length) % available.length];
    } else if (key.length === 1 && !event.metaKey && !event.ctrlKey) {
      const now = Date.now(); prefix = now - lastKey > 1000 ? key : prefix + key; lastKey = now;
      const next = options.findIndex(option => !option.heading && option.label.toLowerCase().startsWith(prefix.toLowerCase()));
      if (next < 0) return;
      current = next;
    } else return;
    items.forEach((item, i) => { if (item) item.tabIndex = i === current ? 0 : -1; }); items[current].focus();
  });
  document.querySelectorAll('.popup-menu').forEach(existing => existing.dispatchEvent(new Event('dismiss')));
  menu.addEventListener('dismiss', () => close(false));
  document.body.append(menu);
  const rect = button.getBoundingClientRect();
  menu.style.left = `${Math.max(6, Math.min(rect.right - menu.offsetWidth, innerWidth - menu.offsetWidth - 6))}px`;
  menu.style.top = `${Math.min(rect.bottom + 3, innerHeight - menu.offsetHeight - 6)}px`;
  document.addEventListener('pointerdown', outside, true);
  document.addEventListener('keydown', escape, true);
  items[current]?.focus();
  return menu;
}

export function makeControl(row, state, save) {
  const kind = controlFor(row.kind);
  if (kind === 'file') return element('output', 'file-value', displayValue(state.value) || 'Not set');
  if (kind === 'switch') {
    const button = element('button', 'switch');
    button.type = 'button'; button.setAttribute('role', 'switch');
    button.setAttribute('aria-label', state.label);
    button.setAttribute('aria-checked', String(state.value === true));
    if (state.value == null) button.dataset.unset = 'true';
    button.disabled = !state.editable;
    button.append(element('span', 'switch-thumb'));
    button.addEventListener('click', () => save(String(state.value !== true)));
    return button;
  }
  if (kind === 'popup') {
    const button = element('button', 'choice', displayValue(state.value) || 'Not set');
    button.type = 'button'; button.setAttribute('aria-label', state.label);
    button.setAttribute('aria-haspopup', 'menu'); button.disabled = !state.editable;
    button.append(element('span', 'popup-arrow', '▾'));
    button.addEventListener('click', () => popup(button, row.choices.map(value => ({ label: String(value), value, selected: value === state.value })), value => save(cliValue(value))));
    return button;
  }
  const input = element('input', kind === 'stepper' ? 'number-field' : 'text-field');
  input.type = 'text'; input.value = displayValue(state.value); input.placeholder = 'Not set';
  input.setAttribute('aria-label', state.label); input.readOnly = !state.editable;
  input.dataset.settingField = 'true'; input.dataset.kind = row.kind;
  const commit = fieldCommit(input.value, value => save(value));
  const finish = () => {
    if (kind === 'stepper' && input.value.trim() !== '' && Number.isFinite(Number(input.value)) && row.minimum != null) input.value = String(Math.max(row.minimum, Number(input.value)));
    commit(input.value);
  };
  input.addEventListener('blur', finish);
  input.addEventListener('keydown', event => {
    if (event.key === 'Enter') { finish(); input.blur(); }
    if (event.key === 'Escape') { input.value = displayValue(state.value); input.blur(); }
  });
  if (kind === 'stepper') {
    input.setAttribute('role', 'spinbutton'); input.inputMode = row.kind === 'int' ? 'numeric' : 'decimal';
    if (row.minimum != null) input.setAttribute('aria-valuemin', row.minimum);
    if (state.value != null) input.setAttribute('aria-valuenow', state.value);
    input.setAttribute('aria-valuetext', `${displayValue(state.value) || 'Not set'}, ${row.source}`);
    const wrapper = element('span', 'stepper'); wrapper.append(input);
    const step = direction => {
      if (!state.editable) return;
      const next = Math.max(row.minimum ?? -Infinity, Number(input.value || row.minimum || 0) + direction * (row.kind === 'float' ? .05 : 1));
      input.value = String(Number(next.toFixed(4))); input.setAttribute('aria-valuenow', input.value);
    };
    input.addEventListener('keydown', event => { if (event.key === 'ArrowUp' || event.key === 'ArrowDown') { event.preventDefault(); step(event.key === 'ArrowUp' ? 1 : -1); } });
    const buttons = element('span', 'step-buttons');
    for (const [direction, label, glyph] of [[1, 'Increase', '▴'], [-1, 'Decrease', '▾']]) {
      const button = element('button', '', glyph); button.type = 'button'; button.tabIndex = -1;
      button.setAttribute('aria-label', `${label} ${state.label}`); button.disabled = !state.editable;
      button.addEventListener('mousedown', event => event.preventDefault());
      button.addEventListener('click', () => { step(direction); finish(); }); buttons.append(button);
    }
    wrapper.append(buttons); return wrapper;
  }
  if (kind === 'new-items') {
    const button = element('button', 'choice', typeof state.value === 'boolean' ? state.value ? 'All' : 'None' : 'Selected kinds');
    button.type = 'button'; button.disabled = !state.editable; button.setAttribute('aria-label', state.label);
    const wrapper = element('span', 'new-items-control');
    button.addEventListener('click', () => popup(button, [{ label: 'All', value: 'true' }, { label: 'None', value: 'false' }, { label: 'Selected kinds…', value: null }], value => value == null ? (input.hidden = false, input.focus()) : save(value)));
    input.hidden = !Array.isArray(state.value); wrapper.append(button, input); return wrapper;
  }
  return input;
}

// Highlight only rendered words. Keep controls, event targets and tooltip data intact.
export function highlightMatches(root, query) {
  if (!query) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT), nodes = [];
  while (walker.nextNode()) if (!walker.currentNode.parentElement.closest('[hidden], input, textarea')) nodes.push(walker.currentNode);
  const needle = query.toLowerCase();
  for (const node of nodes) {
    const text = node.textContent, lower = text.toLowerCase(); let start = 0, at = lower.indexOf(needle);
    if (at < 0) continue;
    const fragment = document.createDocumentFragment();
    while (at >= 0) {
      fragment.append(document.createTextNode(text.slice(start, at)), element('mark', '', text.slice(at, at + query.length)));
      start = at + query.length; at = lower.indexOf(needle, start);
    }
    fragment.append(document.createTextNode(text.slice(start))); node.replaceWith(fragment);
  }
}
