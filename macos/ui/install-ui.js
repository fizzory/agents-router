import { getInstallStatus, onInstallStatus, installAction } from './tauri.js';
import { installDialog, dialogKey } from './logic/install.js';
import { element as el } from './settings-controls.js';
export class InstallUI {
  constructor(changed) { this.changed = changed; this.status = {}; this.previous = null; }
  async start() {
    await onInstallStatus(status => this.render(status));
    this.render(await getInstallStatus());
  }
  async action(action) {
    const form = this.sheet?.querySelector('form');
    if (form?.getAttribute('aria-busy') === 'true') return;
    if (form) { form.setAttribute('aria-busy', 'true'); form.querySelectorAll('button').forEach(b => { b.disabled = true; }); }
    try { await installAction(action); this.render(await getInstallStatus()); }
    catch (error) {
      this.key = null; this.render(await getInstallStatus());
      const message = this.sheet?.querySelector('.sheet-message');
      if (message) message.textContent = String(error); else throw error;
    }
  }
  render(status) {
    this.status = status; this.changed(status);
    const model = installDialog(status);
    const key = JSON.stringify(model);
    if (key === this.key) return;
    this.key = key;
    const hadSheet = !!this.sheet;
    this.sheet?.remove(); this.sheet = null;
    if (!model) { document.querySelector('.sidebar').inert = false; document.querySelector('.main').inert = false; this.previous?.focus(); this.previous = null; return; }
    if (!hadSheet) this.previous = document.activeElement;
    const backdrop = el('div', 'sheet-backdrop'), sheet = el('form', 'account-sheet install-sheet');
    if (model.busy) sheet.setAttribute('aria-busy', 'true');
    sheet.setAttribute('role', 'alertdialog'); sheet.setAttribute('aria-modal', 'true'); sheet.setAttribute('aria-labelledby', 'install-title'); sheet.tabIndex = -1;
    const title = el('strong', '', model.title); title.id = 'install-title';
    const message = el('div', 'sheet-message', model.detail || ''); message.id = 'install-detail'; message.setAttribute('role', 'status'); sheet.setAttribute('aria-describedby', message.id);
    const actions = el('div', 'sheet-actions');
    for (const [label, action] of model.buttons) {
      const button = el('button', action === model.default ? 'primary-button' : '', label); button.type = 'button'; button.dataset.action = action;
      button.addEventListener('click', () => this.action(action)); actions.append(button);
    }
    sheet.addEventListener('submit', event => event.preventDefault());
    sheet.addEventListener('keydown', event => {
      const action = dialogKey(model, event.key);
      if (action) { event.preventDefault(); this.action(action); }
      else if (event.key === 'Tab') {
        const nodes = [...sheet.querySelectorAll('button:not(:disabled)')];
        const index = nodes.indexOf(document.activeElement), next = event.shiftKey ? (index <= 0 ? nodes.length - 1 : index - 1) : (index + 1) % nodes.length;
        event.preventDefault(); nodes[next]?.focus();
      }
    });
    sheet.append(title, message, actions); backdrop.append(sheet); document.body.append(backdrop); this.sheet = backdrop;
    // Keep the app behind the sheet inert, including the sidebar and toolbar.
    document.querySelector('.sidebar').inert = true; document.querySelector('.main').inert = true;
    const focus = sheet.querySelector(`[data-action="${model.default}"]`) || sheet; focus.focus();
  }
}
