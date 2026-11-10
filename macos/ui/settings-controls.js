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
  const paths = { undo: ['M9 14 4 9l5-5', 'M4 9h10.5a5.5 5.5 0 0 1 0 11H11'], search: ['M21 21l-4.3-4.3', 'M19 11a8 8 0 1 1-16 0 8 8 0 0 1 16 0'], lock: ['M6 11h12v10H6z', 'M8 11V7a4 4 0 0 1 8 0v4'], chevron: ['m9 18 6-6-6-6'] };
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
  let current = Math.max(0, options.findIndex(item => item.selected));
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
      current = key === 'Home' ? 0 : key === 'End' ? items.length - 1 : (current + (key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
    } else if (key.length === 1 && !event.metaKey && !event.ctrlKey) {
      const now = Date.now(); prefix = now - lastKey > 1000 ? key : prefix + key; lastKey = now;
      const next = options.findIndex(option => option.label.toLowerCase().startsWith(prefix.toLowerCase()));
      if (next < 0) return;
      current = next;
    } else return;
    items.forEach((item, i) => { item.tabIndex = i === current ? 0 : -1; }); items[current].focus();
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
