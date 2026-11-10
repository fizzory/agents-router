import * as api from './tauri.js';
import { element as el, icon, makeControl, popup } from './settings-controls.js';
import { rowState, errorMessage, displayValue, PROVIDERS, LEVELS } from './logic/settings.js';
import { orderedRole, roleName, roleChoices, roleSource, moveProvider, accountModels, AccountNameCheck,
  sharingModel, shareResult, SHARE_LABELS, SHARE_ORDER, SHARE_MORE, filterActivity } from './logic/panes.js';
const find = selector => document.querySelector(selector);
const button = (text, action, className = 'plain-button') => {
  const node = el('button', className, text); node.type = 'button'; node.addEventListener('click', action); return node;
};
const switchButton = (label, value, disabled, action) => makeControl({ kind: 'bool' }, { label, value, editable: !disabled }, action);
const match = (query, ...text) => text.join(' ').toLowerCase().includes(query.toLowerCase());

export class SettingsPanes {
  constructor(host) {
    this.host = host; this.data = {}; this.errors = new Map(); this.results = new Map(); this.loading = new Set(); this.busy = false;
    this.roving = new Map(); this.menuSerial = 0; this.focusAccount = null; this.flashKey = null; this.seenEvents = new Set();
    this.nameCheck = new AccountNameCheck(api.accountsAddCheck);
    this.root = find('#pane-content');
    this.ready = api.onPaneMenu(({ token, action }) => {
      if (this.menu?.token === token) { const run = this.menu.actions[action]; this.menu = null; if (run) this.run(run); }
    });
  }
  pane() { return document.body.dataset.pane; }
  run(work) { return Promise.resolve().then(work).catch(error => this.host.report(errorMessage(error))); }
  async context(event, actions) {
    event.preventDefault();
    const token = String(++this.menuSerial);
    this.menu = { token, actions };
    await api.showPaneMenu(token, Object.keys(actions));
  }
  async load(pane = this.pane(), force = false) {
    if (pane === 'general') return;
    const workspace = pane === 'roles' ? this.host.scope().workspace : null;
    const key = pane === 'roles' ? `roles:${workspace}` : pane;
    if (this.loading.has(key) || (!force && this.data[key])) { this.render(); return; }
    this.loading.add(key); this.errors.delete(pane); this.render();
    try {
      if (pane === 'roles') this.data[key] = await api.rolesShow(workspace);
      else if (pane === 'accounts') this.data.accounts = await api.accountsList();
      else if (pane === 'sharing') {
        const [listing, status, config] = await Promise.all([api.shareList(), api.shareStatus(), api.configShow(null)]);
        this.data.sharing = { listing, status, config };
        find('#sharing-dot').hidden = !sharingModel(listing, status).waiting.length;
      } else if (pane === 'activity') this.data.activity = await api.activityLog();
    } catch (error) { this.errors.set(pane, errorMessage(error)); }
    finally { this.loading.delete(key); this.host.render(); }
  }
  async badge() {
    try { const status = await api.shareStatus(); find('#sharing-dot').hidden = !(status.attention || []).length; } catch { /* Sharing owns its Retry state. */ }
  }
  async refresh() { await this.load(this.pane(), true); }
  async mutate(key, work, pane = this.pane()) {
    if (this.busy || this.host.busy()) return;
    this.busy = true; this.errors.delete(key); this.host.render();
    try {
      const result = await work();
      if (result?.ok === false || result?.error || result?.needs_user_words) this.errors.set(key, errorMessage(result));
      return result;
    } catch (error) { this.errors.set(key, errorMessage(error)); }
    finally { this.busy = false; await this.load(pane, true); this.host.render(); }
  }
  inlineError(node, key) {
    if (!this.errors.has(key)) return;
    const error = el('div', 'inline-error', this.errors.get(key)); error.setAttribute('role', 'alert'); node.append(error);
  }
  render() {
    const pane = this.pane(), query = find('#search').value.trim();
    const active = document.activeElement, owner = active?.closest('[data-key]');
    const focus = this.root.contains(active) && owner ? { key: owner.dataset.key, entry: active.dataset.entry,
      className: active.classList[0], tag: active.tagName.toLowerCase() } : null;
    this.root.hidden = pane === 'general';
    this.root.replaceChildren();
    if (pane === 'general') return;
    const key = pane === 'roles' ? `roles:${this.host.scope().workspace}` : pane;
    const data = this.data[key];
    if (this.errors.has(pane)) {
      const row = el('div', 'banner error-banner');
      const message = el('span', '', "Can't read agt"); message.dataset.tooltip = this.errors.get(pane);
      row.append(message, button('Retry', () => this.run(() => this.load(pane, true)))); this.root.append(row); return;
    }
    if (!data) { this.root.append(el('p', 'empty-pane', 'Reading agt…')); return; }
    if (pane === 'roles') this.renderRoles(data, query);
    if (pane === 'accounts') this.renderAccounts(data, query);
    if (pane === 'sharing') this.renderSharing(data, query);
    if (pane === 'activity') this.renderActivity(data, query);
    if (focus) {
      const row = [...this.root.querySelectorAll('[data-key]')].find(n => n.dataset.key === focus.key);
      const selector = focus.entry ? `.provider-chip[data-entry="${CSS.escape(focus.entry)}"]` : focus.className ? `${focus.tag}.${CSS.escape(focus.className)}` : focus.tag;
      row?.querySelector(selector)?.focus({ preventScroll: true });
    }
    if (this.flashKey) {
      const row = [...this.root.querySelectorAll('[data-key]')].find(node => node.dataset.key === this.flashKey);
      if (row) { row.classList.add('setting-flash'); row.scrollIntoView({ block: 'nearest' }); row.focus({ preventScroll: true }); this.flashKey = null; }
    }
  }
  noMatch(query, count, text) {
    if (!count) this.root.append(el('p', 'empty-pane', query ? 'No match' : text));
  }
  roleWrite(role, edit) {
    const scope = { ...this.host.scope() };
    return this.mutate(`roles.${role.name}`, () => this.host.history.commitRole({ edit, ...scope }, { pane: 'roles', key: `roles.${role.name}`, ...scope }), 'roles');
  }
  async move(role, from, to) {
    const move = moveProvider(role, from, to); if (!move) return;
    const entry = role.providers[from].entry;
    // Keep the dragged chip's identity focused when the fresh document arrives.
    this.roving.set(role.name, to);
    const old = new Map([...this.root.querySelectorAll(`[data-role="${CSS.escape(role.name)}"] .provider-chip`)].map(n => [n.dataset.entry, n.getBoundingClientRect()]));
    const result = await this.roleWrite(role, move.edit);
    if (!result || result.error || result.needs_user_words) return;
    find('#announcement').textContent = move.announcement;
    const chips = [...this.root.querySelectorAll('.provider-chip')].filter(n => n.dataset.role === role.name);
    if (!matchMedia('(prefers-reduced-motion: reduce)').matches) for (const chip of chips) {
      const previous = old.get(chip.dataset.entry); if (!previous) continue;
      const rect = chip.getBoundingClientRect(); chip.style.transform = `translate(${previous.left - rect.left}px, ${previous.top - rect.top}px)`;
      requestAnimationFrame(() => { chip.classList.add('row-moving'); chip.style.transform = ''; });
    }
    chips.find(n => n.dataset.entry === entry)?.focus();
  }
  chipMenu(chip, role, provider, index) {
    const choices = roleChoices(provider), options = [{ label: 'All roles', heading: true }];
    for (const [kind, label, values] of [['model', 'Model', choices.models], ['effort', 'Effort', choices.efforts]]) {
      options.push({ label, heading: true });
      for (const choice of values) options.push({ ...choice, value: { kind, entry: provider.entry, value: choice.value } });
    }
    const menu = popup(chip, options, edit => this.run(() => this.roleWrite(role, edit)));
    if (!role.enabled) menu.classList.add('role-off-menu');
  }
  renderRoles(data, query) {
    let count = 0;
    if (data.error) { const row = el('div', 'banner error-banner', errorMessage(data)); this.root.append(row); }
    for (const role of data.roles) {
      if (!match(query, role.name, role.use_when, ...role.providers.map(p => p.entry))) continue;
      count++;
      const row = el('div', 'role-row'); row.dataset.key = `roles.${role.name}`; row.dataset.role = role.name; row.tabIndex = -1;
      row.dataset.tooltip = role.use_when || role.name;
      const enabled = switchButton(roleName(role.name), role.enabled, this.busy || this.host.busy() || !!data.error, () => this.run(() => this.roleWrite(role, { kind: 'enable', role: role.name, enabled: !role.enabled })));
      row.append(enabled, el('span', 'role-name', role.name));
      const chips = el('div', 'provider-chips'); chips.setAttribute('role', 'group'); chips.setAttribute('aria-label', `${roleName(role.name)} providers`);
      chips.classList.toggle('role-off', !role.enabled);
      const current = Math.min(this.roving.get(role.name) || 0, role.providers.length - 1);
      role.providers.forEach((provider, index) => {
        const chip = button('', () => this.chipMenu(chip, role, provider, index), 'provider-chip');
        chip.dataset.entry = provider.entry; chip.dataset.role = role.name; chip.tabIndex = index === current ? 0 : -1;
        chip.disabled = this.busy || this.host.busy() || !!data.error;
        chip.draggable = orderedRole(role) && !chip.disabled;
        chip.classList.toggle('unusable', !provider.usable);
        chip.dataset.tooltip = [provider.entry, provider.model, provider.effort, provider.reason].filter(Boolean).join('\n');
        chip.setAttribute('aria-label', `${provider.entry}${orderedRole(role) ? `, position ${index + 1} of ${role.providers.length}` : ''}${provider.reason ? `. ${provider.reason}` : ''}`);
        if (orderedRole(role)) chip.append(el('span', 'chip-order', index + 1));
        if (provider.entry.includes(':')) { const dot = el('span', 'dot'); dot.dataset.state = provider.usable ? 'ok' : 'unhealthy'; chip.append(dot); }
        chip.append(document.createTextNode(provider.entry));
        chip.addEventListener('focus', () => { this.roving.set(role.name, index); [...chips.children].forEach(n => { n.tabIndex = n === chip ? 0 : -1; }); });
        chip.addEventListener('keydown', event => {
          if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return;
          event.preventDefault();
          const next = event.key === 'Home' ? 0 : event.key === 'End' ? role.providers.length - 1 : index + (['ArrowLeft', 'ArrowUp'].includes(event.key) ? -1 : 1);
          if (event.altKey && orderedRole(role)) this.run(() => this.move(role, index, next));
          else chips.children[Math.max(0, Math.min(role.providers.length - 1, next))]?.focus();
        });
        chip.addEventListener('contextmenu', event => {
          const actions = {};
          if (orderedRole(role) && !this.busy && !data.error) {
            if (index > 0) { actions['move-front'] = () => this.move(role, index, 0); actions['move-up'] = () => this.move(role, index, index - 1); }
            if (index < role.providers.length - 1) actions['move-down'] = () => this.move(role, index, index + 1);
          }
          if (Object.keys(actions).length) this.run(() => this.context(event, actions)); else event.preventDefault();
        });
        chip.addEventListener('dragstart', event => { this.drag = { role: role.name, from: index }; event.dataTransfer.effectAllowed = 'move'; event.dataTransfer.setData('text/plain', provider.entry); event.dataTransfer.setDragImage(chip, 10, 10); });
        chip.addEventListener('dragover', event => { if (this.drag?.role === role.name && orderedRole(role)) { event.preventDefault(); event.dataTransfer.dropEffect = 'move'; } });
        chip.addEventListener('drop', event => { event.preventDefault(); if (this.drag?.role === role.name) this.run(() => this.move(role, this.drag.from, index)); this.drag = null; });
        chip.addEventListener('dragend', () => { this.drag = null; }); chips.append(chip);
      });
      row.append(chips);
      const slot = el('span', 'row-slot'), source = roleSource(role, this.host.scope().level);
      if (source) {
        const chip = button(source.text, () => this.run(() => this.host.navigate({ pane: 'roles', level: source.level, workspace: this.host.scope().workspace, key: row.dataset.key })), 'source-chip');
        chip.dataset.tooltip = `${source.key}: ${displayValue(source.value)}\n${source.file}:${source.line || 1}`; slot.append(chip);
      }
      row.append(slot); this.inlineError(row, row.dataset.key); this.root.append(row);
    }
    this.noMatch(query, count, 'No roles');
    if (!data.roles.length && !query) {
      const setup = button('Set up', () => this.terminal('setupRoles', null, null, setup)); this.root.append(setup);
    }
  }
  terminal(action, runtime, name, node) {
    return this.run(async () => {
      const delay = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--terminal-delay'));
      const timer = setTimeout(() => { node.setAttribute('aria-busy', 'true'); node.textContent = 'Opening Terminal…'; }, delay);
      try { await api[action](runtime, name); }
      finally { clearTimeout(timer); node.removeAttribute('aria-busy'); this.host.render(); }
    });
  }
  async checkAccount(model) {
    this.checking = model.key;
    try { return await this.mutate(model.key, () => api.accountsCheck(model.runtime, model.account.name), 'accounts'); }
    finally { this.checking = null; this.host.render(); }
  }
  renderAccounts(data, query) {
    const models = accountModels(data, this.host.snapshot().usage, Date.now() / 1000);
    let count = 0;
    for (const model of models) {
      const { account } = model;
      if (!match(query, model.provider, account.name, model.tooltip)) continue;
      count++;
      const row = el('div', 'settings-account'); row.dataset.key = model.key; row.tabIndex = -1; row.dataset.tooltip = model.tooltip; row.setAttribute('aria-label', model.tooltip);
      const dot = el('span', 'dot'); dot.dataset.state = model.availability; dot.setAttribute('aria-hidden', 'true');
      row.append(dot, el('span', 'provider', model.providerFirst ? model.provider : ''));
      const name = el('span', 'account-name', model.name); name.title = account.name;
      if (model.toggle.kind === 'badge') {
        const badge = el('span', 'account-badge', model.toggle.text); badge.tabIndex = 0; badge.setAttribute('role', 'note'); badge.dataset.tooltip = model.toggle.reason;
        badge.setAttribute('aria-label', `${model.provider} ${account.name}. ${model.toggle.reason}`);
        if (account.name === 'primary') name.append(badge); else name.append(document.createTextNode(' '), badge);
      }
      row.append(name);
      const meter = el('span', 'twin-meter'); meter.setAttribute('aria-hidden', 'true');
      for (const window of model.meters) {
        const track = el('span', `meter-track${window.dominant ? ' dominant' : ''}${window.dimmed ? ' dimmed' : ''}`), fill = el('span', 'meter-fill');
        fill.style.transform = `translateX(${window.percent - 100}%)`; track.append(fill); meter.append(track);
      }
      meter.classList.toggle('no-windows', !model.hasWindows); row.append(meter);
      const slot = el('span', 'account-slot');
      if (model.action) {
        const action = button(model.button, () => model.action === 'login'
          ? this.terminal('accountsLogin', model.runtime, account.name, action)
          : this.run(() => this.checkAccount(model)), 'inline-action');
        action.setAttribute('aria-label', `${model.button} for ${model.provider} ${account.name}`); action.disabled = this.busy;
        if (this.checking === model.key) {
          action.setAttribute('aria-busy', 'true'); action.setAttribute('aria-label', `Checking ${model.provider} ${account.name}`);
          action.replaceChildren(icon('refresh')); action.firstChild.classList.add('turning');
          action.firstChild.addEventListener('animationend', event => { if (this.checking === model.key && event.target.isConnected) { event.target.classList.remove('turning'); requestAnimationFrame(() => event.target.classList.add('turning')); } });
        }
        slot.append(action);
      } else if (model.figure) {
        const figure = el('span', `percent${model.threshold ? ' threshold' : ''}`, model.figure); slot.append(figure, el('span', 'reset-text', model.reset));
        slot.classList.toggle('dimmed', model.dimmed);
      } else slot.append(el('span', 'state-word', model.state));
      row.append(slot);
      const toggle = el('span', 'account-toggle');
      if (model.toggle.kind === 'switch') toggle.append(switchButton(model.toggle.label, account.enabled, this.busy, () => this.run(() => this.mutate(model.key, () => api.accountsToggle(model.runtime, account.name, !account.enabled), 'accounts'))));
      row.append(toggle);
      const loginSlot = el('span', 'account-login-slot');
      if (model.action !== 'login') {
        const login = button('Log in', () => this.terminal('accountsLogin', model.runtime, account.name, login), 'login-action');
        login.setAttribute('aria-label', `Log in to ${model.provider} ${account.name}`); loginSlot.append(login);
      }
      row.append(loginSlot);
      if (model.reason && model.state) row.append(el('span', 'account-reason', model.reason));
      row.addEventListener('contextmenu', event => {
        const actions = { login: () => api.accountsLogin(model.runtime, account.name) };
        if (account.can_toggle) actions[account.enabled ? 'disable' : 'enable'] = () => this.mutate(model.key, () => api.accountsToggle(model.runtime, account.name, !account.enabled), 'accounts');
        this.run(() => this.context(event, actions));
      });
      this.inlineError(row, model.key); this.root.append(row);
    }
    this.noMatch(query, count, 'No accounts');
    this.root.append(button('Add account', () => this.openAccountSheet(), 'plain-button add-account'));
  }
  openAccountSheet() {
    if (find('.sheet-backdrop')) return;
    const previous = document.activeElement, backdrop = el('div', 'sheet-backdrop'), sheet = el('form', 'account-sheet');
    sheet.setAttribute('role', 'alertdialog'); sheet.setAttribute('aria-modal', 'true'); sheet.setAttribute('aria-labelledby', 'add-account-title');
    sheet.append(el('strong', '', 'Add account')); sheet.firstChild.id = 'add-account-title';
    let runtime = 'claude', pending = false;
    const providerRow = el('div', 'sheet-field'), providerLabel = el('span', '', 'Provider');
    const provider = button('Claude ▾', () => popup(provider, Object.entries(PROVIDERS).filter(([id]) => id !== 'kiro').map(([value, label]) => ({ label, value, selected: runtime === value })), value => {
      runtime = value; provider.textContent = `${PROVIDERS[value]} ▾`; validate();
    }), 'choice'); provider.setAttribute('aria-label', 'Provider'); providerRow.append(providerLabel, provider);
    const nameRow = el('div', 'sheet-field'), label = el('label', '', 'Name'), input = el('input', 'text-field');
    input.id = 'new-account-name'; input.type = 'text'; input.autocomplete = 'off'; input.spellcheck = false; label.htmlFor = input.id;
    const message = el('div', 'sheet-message'); message.id = 'account-name-message'; message.setAttribute('role', 'status'); input.setAttribute('aria-describedby', message.id);
    const actions = el('div', 'sheet-actions');
    const close = () => { if (pending) return; this.nameCheck.invalidate(); document.querySelectorAll('.popup-menu').forEach(n => n.dispatchEvent(new Event('dismiss'))); backdrop.remove(); find('.main').inert = false; find('.sidebar').inert = false; previous?.focus(); };
    const cancel = button('Cancel', close), add = button('Add account', () => {}, 'plain-button primary-button'); add.type = 'submit'; add.disabled = true;
    const validate = async () => {
      this.nameCheck.invalidate(); add.disabled = true; input.removeAttribute('aria-invalid'); message.textContent = input.value.trim() ? 'Checking name…' : '';
      const state = await this.nameCheck.validate(runtime, input.value);
      if (!state || !backdrop.isConnected) return;
      add.disabled = !state.valid || pending; message.textContent = state.message;
      if (state.message) input.setAttribute('aria-invalid', 'true');
    };
    input.addEventListener('input', validate);
    sheet.addEventListener('submit', async event => {
      event.preventDefault(); if (add.disabled || pending) return;
      pending = true; add.disabled = true; cancel.disabled = true; provider.disabled = true; input.readOnly = true;
      try { await api.accountsAdd(runtime, input.value); pending = false; close(); }
      catch (error) { pending = false; cancel.disabled = false; provider.disabled = false; input.readOnly = false; this.nameCheck.invalidate(); add.disabled = true; message.textContent = errorMessage(error); input.focus(); }
    });
    sheet.addEventListener('keydown', event => {
      if (event.key === 'Escape') { event.preventDefault(); close(); }
      if (event.key === 'Tab') {
        const nodes = [...sheet.querySelectorAll('button:not(:disabled),input:not(:disabled)')];
        if (event.shiftKey && document.activeElement === nodes[0]) { event.preventDefault(); nodes.at(-1).focus(); }
        else if (!event.shiftKey && document.activeElement === nodes.at(-1)) { event.preventDefault(); nodes[0].focus(); }
      }
    });
    actions.append(cancel, add); nameRow.append(label, input); sheet.append(providerRow, nameRow, message, actions); backdrop.append(sheet); document.body.append(backdrop);
    find('.main').inert = true; find('.sidebar').inert = true; input.focus();
  }
  copyButton(text, value, tooltip = '') {
    const copy = button(text, () => this.run(async () => {
      await api.copyText(value); copy.textContent = 'Copied';
      setTimeout(() => { copy.textContent = text; }, parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--copied-delay')));
    })); copy.dataset.tooltip = tooltip || value; copy.setAttribute('aria-label', text === 'Copy command' ? `Copy command: ${value}` : text); return copy;
  }
  sharingRow(row, model) {
    const node = el('div', 'sharing-row'); node.dataset.key = row.key; node.tabIndex = -1; node.dataset.tooltip = `${row.key}\n${row.description}\n${row.applies}`;
    const label = SHARE_LABELS[row.key] || row.key, value = row.levels.user.present ? row.levels.user.value : row.value;
    node.append(el('span', 'sharing-label', label));
    const state = { ...rowState(row, 'user', this.data.sharing.config), value, label, editable: model.enabled && row.editable && !this.busy && !this.host.busy() };
    if (row.kind === 'fixed') {
      node.append(el('output', 'file-value', displayValue(value)), icon('lock'), this.copyButton('Copy command', this.fixedCommand(row.key)));
    } else if (row.kind === 'auto_copy') {
      const control = button(value === true ? 'Every CLI ▾' : value === false ? 'Wait for a choice ▾' : `${displayValue(value)} ▾`, () => popup(control, [
        { label: 'Every CLI', value: 'on', selected: value === true }, { label: 'Wait for a choice', value: 'off', selected: value === false },
        ...['agents', 'skills', 'mcp', 'hooks', 'plugins'].map(kind => ({ label: kind, value: kind, selected: Array.isArray(value) && value.includes(kind) }))
      ], next => this.run(() => this.saveShared(row, next))), 'choice'); control.disabled = !state.editable; control.setAttribute('aria-label', label); node.append(control);
    } else node.append(makeControl(row, state, next => this.run(() => ['share.mcp', 'share.hooks'].includes(row.key)
      ? this.mutate(row.key, async () => { const report = await api.shareKind(row.key.split('.')[1], next === 'true'); this.results.set(row.key, shareResult(report)); return report; }, 'sharing')
      : this.saveShared(row, next))));
    if (this.results.has(row.key)) { const result = el('div', 'sharing-result', this.results.get(row.key)); result.setAttribute('role', 'status'); node.append(result); }
    this.inlineError(node, row.key); return node;
  }
  fixedCommand(key) {
    return { 'share.enabled': 'agt share enable --dry-run', 'share.kinds': 'agt share kinds --help', 'share.plugins': 'agt share kinds add plugins --dry-run' }[key];
  }
  saveShared(row, value) {
    return this.mutate(row.key, () => this.host.history.commit({ key: row.key, value, level: 'user', workspace: null, revision: row.levels.user.revision }, { pane: 'sharing', level: 'user', workspace: null }), 'sharing');
  }
  renderSharing(data, query) {
    const model = sharingModel(data.listing, data.status);
    if (model.state) { const state = el('div', 'banner sharing-state'); state.append(el('span', '', model.state), this.copyButton('Copy command', model.command)); this.root.append(state); }
    const settings = data.config.settings.filter(r => r.key.startsWith('share.'));
    const add = (key, parent = this.root) => {
      const row = settings.find(r => r.key === key); if (!row || !match(query, SHARE_LABELS[key], key, row.description)) return 0;
      parent.append(this.sharingRow(row, model)); return 1;
    };
    let count = 0;
    for (const key of SHARE_ORDER) { if (key === 'share.watcher') this.root.append(el('hr', 'pane-divider')); count += add(key); }
    const more = el('details', 'sharing-more'); more.append(el('summary', '', `More (${SHARE_MORE.length})`));
    for (const key of SHARE_MORE) count += add(key, more);
    if (query) more.open = true; this.root.append(more);
    if (model.waiting.length) {
      const waiting = el('details', 'waiting-items'); waiting.append(el('summary', '', `Waiting (${model.waiting.length})`));
      for (const item of model.waiting) {
        if (query && !match(query, item.message, item.item)) continue;
        const row = el('div', 'waiting-row'); row.append(el('span', '', item.message), this.copyButton('Copy command', item.command)); waiting.append(row);
      }
      this.root.append(waiting);
    }
    const items = el('div', 'sharing-items');
    for (const item of model.items) {
      if (!match(query, item.name, item.kind, item.id)) continue;
      count++;
      const row = el('div', 'share-item'); row.dataset.tooltip = item.id; row.append(el('span', 'share-item-name', item.name));
      const cells = el('div', 'share-cells');
      for (const cell of item.cells) {
        const node = el('span', 'share-cell'); node.append(el('span', '', PROVIDERS[data.listing.folders.find(f => f.label === cell.label)?.cli] || cell.label));
        if (cell.editable) node.append(switchButton(`${item.name} in ${cell.label}`, cell.included, !model.enabled || this.busy, () => this.run(() => this.mutate(item.id,
          () => api.sharePick(item.id, cell.label, !cell.included), 'sharing'))));
        else { const copy = this.copyButton('Copy command', cell.command, cell.reason); copy.classList.add('cell-copy'); node.append(copy); }
        node.dataset.tooltip = cell.reason; cells.append(node);
      }
      row.append(cells); this.inlineError(row, item.id); items.append(row);
    }
    this.root.append(items);
    const fixed = el('details', 'sharing-fixed'); fixed.append(el('summary', '', 'File only'));
    for (const row of settings.filter(r => r.kind === 'fixed')) count += add(row.key, fixed);
    this.root.append(fixed); this.noMatch(query, count, 'No shared items');
  }
  renderActivity(data, query) {
    const models = filterActivity(data.entries || [], query, this.host.config()?.settings || []);
    this.noMatch(query, models.length, 'Nothing logged yet.');
    for (const model of models) {
      const { entry } = model;
      const row = button('', () => {
        if (entry.key) this.run(() => this.host.navigate({ pane: entry.key.startsWith('share.') ? 'sharing' : entry.key.startsWith('roles.') ? 'roles' : 'general',
          key: entry.key, level: entry.level || 'user', workspace: entry.level === 'user' ? null : entry.workspace || entry.family }));
      }, 'activity-row'); row.dataset.tooltip = model.tooltip; row.dataset.key = entry.id;
      if (!this.seenEvents.has(entry.id) && !matchMedia('(prefers-reduced-motion: reduce)').matches) row.classList.add('row-enter');
      this.seenEvents.add(entry.id);
      row.append(el('time', '', model.time), el('span', 'activity-label', model.label), el('span', 'activity-change', model.change), el('span', 'activity-level', model.level));
      if (model.marker) { const marker = el('span', 'activity-marker'); marker.append(icon('alert'), document.createTextNode(model.marker)); row.append(marker); }
      row.setAttribute('aria-label', [model.time, model.label, model.change, model.level, model.marker].filter(Boolean).join(', '));
      row.addEventListener('contextmenu', event => {
        const actions = {};
        if (entry.type === 'setting') { actions.revert = () => this.mutate(entry.id, () => api.configRevert({ entry: entry.id, workspace: entry.level === 'user' ? null : entry.workspace || entry.family }), 'activity'); actions['copy-key'] = () => api.copyText(entry.key); }
        if (entry.type === 'refused' && entry.file) actions['open-file'] = () => api.openSettingsFile(entry.file, entry.line || 1);
        if (entry.type === 'sync' && entry.changes?.length && entry.changes.every(c => c.item === entry.changes[0].item && c.ok) && entry.changes.some(c => c.backup) && /^(agents|skills)\//.test(entry.changes[0].item) && !entry.workspace && !entry.family) actions['undo-sync'] = () => this.mutate(entry.id, () => api.activityUndoSync(entry.id), 'activity');
        if (Object.keys(actions).length) this.run(() => this.context(event, actions)); else event.preventDefault();
      });
      this.root.append(row); this.inlineError(this.root, entry.id);
    }
  }
}
