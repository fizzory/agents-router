// Render stable rows without interpreting any account text as markup.
export function element(document, tag, className, text) {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
export function createRow(document, onSettings, onAction) {
  const row = element(document, 'li', 'account-row');
  const link = element(document, 'button', 'row-link');
  const dot = element(document, 'span', 'dot');
  const provider = element(document, 'span', 'provider');
  const name = element(document, 'span', 'account-name');
  const start = element(document, 'span', 'name-start');
  const ellipsis = element(document, 'span', 'name-ellipsis', '…');
  const end = element(document, 'span', 'name-end');
  name.append(start, ellipsis, end);
  const meter = element(document, 'span', 'twin-meter');
  const tracks = Array.from({ length: 2 }, () => {
    const track = element(document, 'span', 'meter-track');
    const fill = element(document, 'span', 'meter-fill');
    track.append(fill); meter.append(track);
    return { track, fill };
  });
  const slot = element(document, 'span', 'status-slot');
  const number = element(document, 'span', 'percent');
  const reset = element(document, 'span', 'reset-text');
  const state = element(document, 'span', 'state-word');
  const action = element(document, 'button', 'inline-action');
  slot.append(number, reset, state, action);
  for (const node of [dot, provider, name, meter, number, reset, state]) node.setAttribute('aria-hidden', 'true');
  link.addEventListener('click', () => onSettings(view.model));
  action.addEventListener('click', () => onAction(view.model, action));
  row.append(link, dot, provider, name, meter, slot);
  const view = { row, link, dot, provider, name, start, end, meter, tracks, slot, number, reset, state, action, model: null };
  return view;
}
export function updateRow(view, model, fresh = false, reduced = false) {
  const old = view.model;
  view.model = model;
  if (fresh) view.row.classList.add('is-fresh');
  else if (!old || old.figure !== model.figure || JSON.stringify(old.meters) !== JSON.stringify(model.meters)) view.row.classList.remove('is-fresh');
  view.row.dataset.tooltip = model.tooltip;
  view.link.setAttribute('aria-label', `${model.tooltip}. Open account in Settings.`);
  view.link.tabIndex = model.action ? -1 : 0;
  view.dot.dataset.state = model.availability;
  view.provider.textContent = model.providerFirst ? model.provider : '';
  const split = Math.ceil(model.name.length / 2);
  view.start.textContent = model.name.slice(0, split);
  view.end.textContent = model.name.slice(split);
  view.meter.classList.toggle('no-windows', !model.hasWindows);
  model.meters.forEach((window, index) => {
    const { track, fill } = view.tracks[index];
    track.classList.toggle('dominant', window.dominant);
    track.classList.toggle('warning', window.percent >= 95);
    track.classList.toggle('dimmed', window.dimmed);
    fill.style.transform = `translateX(${window.percent - 100}%)`;
  });
  view.slot.classList.toggle('dimmed', model.dimmed);
  view.number.classList.toggle('threshold', model.threshold);
  view.number.hidden = !model.figure;
  view.reset.hidden = !model.figure;
  if (!old || old.figure !== model.figure) rollNumber(view.number, old?.figure || '', model.figure, fresh && !reduced);
  view.reset.textContent = model.reset;
  view.state.textContent = model.state;
  view.state.hidden = !model.state;
  view.action.hidden = !model.action;
  // The overlay and the inline action share exactly one sequential tab stop.
  view.action.tabIndex = model.action ? 0 : -1;
  view.action.setAttribute('aria-label', model.accessibleLabel);
  if (view.action.getAttribute('aria-busy') !== 'true') view.action.textContent = model.button;
}
export function fitName(view) {
  view.name.classList.remove('truncated');
  view.name.classList.toggle('truncated', view.start.scrollWidth + view.end.scrollWidth > view.name.clientWidth);
}
export function rollNumber(node, before, after, animate) {
  const document = node.ownerDocument;
  node.replaceChildren();
  if (!animate || !before || !after || before === after) { node.textContent = after; return; }
  const previous = before.padStart(4, ' '), next = after.padStart(4, ' ');
  const changes = [...next].filter((char, i) => char !== previous[i]).length;
  if (changes > 2) {
    node.append(element(document, 'span', 'number-before', before), element(document, 'span', 'number-after', after));
  } else {
    [...next].forEach((char, index) => {
      if (char === previous[index]) { node.append(document.createTextNode(char)); return; }
      const clip = element(document, 'span', 'digit');
      const strip = element(document, 'span', 'digit-strip');
      strip.append(element(document, 'span', '', previous[index]), element(document, 'span', '', char));
      clip.append(strip); node.append(clip);
    });
  }
  node.addEventListener('animationend', () => { node.textContent = after; }, { once: true });
}
