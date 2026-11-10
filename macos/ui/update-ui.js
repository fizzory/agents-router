import { getUpdateStatus, onUpdateStatus, updateAction } from './tauri.js';
import { element as el } from './settings-controls.js';

// Uses the existing Settings sheet anatomy; Rust owns every label and action.
export class UpdateUI {
  async start() {
    await onUpdateStatus(status => this.render(status));
    this.render(await getUpdateStatus());
  }
  async action(action) {
    this.sheet.querySelectorAll('button').forEach(button => { button.disabled = true; });
    try { await updateAction(action); }
    catch (error) {
      this.render(await getUpdateStatus());
      if (this.sheet) this.sheet.querySelector('.sheet-message').textContent = String(error);
    }
  }
  render(status) {
    const model = status.dialog;
    if (this.sheet && model?.busy && this.model?.title === model.title) {
      const meter = this.sheet.querySelector('progress');
      if (status.total) { meter.max = status.total; meter.value = status.received; }
      return;
    }
    const hadSheet = !!this.sheet;
    this.sheet?.remove(); this.sheet = null; this.model = model;
    if (!model) {
      if (hadSheet) {
        const blocked = !!document.querySelector('.sheet-backdrop');
        document.querySelector('.sidebar').inert = blocked;
        document.querySelector('.main').inert = blocked;
        this.previous?.focus(); this.previous = null;
      }
      return;
    }
    this.previous ||= document.activeElement;
    const backdrop = el('div', 'sheet-backdrop'), form = el('form', 'account-sheet install-sheet');
    form.setAttribute('role', 'alertdialog'); form.setAttribute('aria-modal', 'true');
    form.setAttribute('aria-labelledby', 'update-title'); form.tabIndex = -1;
    const title = el('strong', '', model.title); title.id = 'update-title';
    const message = el('div', 'sheet-message', model.detail); message.id = 'update-detail';
    form.setAttribute('aria-describedby', message.id); form.append(title, message);
    if (model.busy) {
      form.setAttribute('aria-busy', 'true');
      const meter = el('progress'); meter.setAttribute('aria-label', model.title); form.append(meter);
    }
    const actions = el('div', 'sheet-actions');
    for (const [label, action] of model.buttons) {
      const button = el('button', action === model.default ? 'primary-button' : '', label);
      button.type = 'button'; button.dataset.action = action;
      button.addEventListener('click', () => this.action(action)); actions.append(button);
    }
    form.append(actions); form.addEventListener('submit', event => event.preventDefault());
    form.addEventListener('keydown', event => {
      if (event.key === 'Enter' && !model.busy) { event.preventDefault(); this.action(model.default); }
      else if (event.key === 'Escape' && model.buttons.some(([, action]) => action === 'close')) { event.preventDefault(); this.action('close'); }
      else if (event.key === 'Tab') {
        const buttons = [...form.querySelectorAll('button:not(:disabled)')], index = buttons.indexOf(document.activeElement);
        event.preventDefault(); buttons[(index + (event.shiftKey ? buttons.length - 1 : 1)) % buttons.length]?.focus();
      }
    });
    backdrop.append(form); document.body.append(backdrop); this.sheet = backdrop;
    document.querySelector('.sidebar').inert = true; document.querySelector('.main').inert = true;
    (form.querySelector(`[data-action="${model.default}"]`) || form).focus();
  }
}
