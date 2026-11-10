import { cliValue, errorMessage } from './settings.js';
import { rolesUndo } from './panes.js';

// Serialized session history. Only agt's `previous` and post-write revision are
// authoritative. Never execute the human-readable `undo` command.
export class SettingsHistory {
  constructor({ set, unset, roles, navigate, reload, report }) {
    Object.assign(this, { set, unset, roles, navigate, reload, report });
    this.undoSteps = [];
    this.redoSteps = [];
    this.tail = Promise.resolve();
  }
  enqueue(work) {
    const result = this.tail.then(work);
    this.tail = result.catch(() => {});
    return result;
  }
  remember(result, target, beforeRevision) {
    return { ...target, key: result.key, file: result.file, level: result.previous.level,
      previous: result.previous, revision: result.revision, beforeRevision };
  }
  commit(args, target, remove = false) {
    return this.enqueue(async () => {
      const result = await (remove ? this.unset(args) : this.set(args));
      if (result.ok && result.changed) {
        this.undoSteps.push(this.remember(result, target, args.revision));
        this.redoSteps.length = 0;
      }
      await this.reload();
      if (!result.ok) this.report(args.key, errorMessage(result));
      return result;
    });
  }
  commitRole(args, target) {
    return this.enqueue(async () => {
      const result = await this.roles(args);
      const step = rolesUndo(result, target);
      if (step) { this.undoSteps.push(step); this.redoSteps.length = 0; }
      await this.reload();
      if (result.error || result.needs_user_words) this.report(target.key, errorMessage(result));
      return result;
    });
  }
  undo() { return this.restore(this.undoSteps, this.redoSteps); }
  redo() { return this.restore(this.redoSteps, this.undoSteps); }
  restore(from, to) {
    return this.enqueue(async () => {
      const step = from.at(-1);
      if (!step) return;
      await this.navigate({ pane: step.pane, level: step.level, workspace: step.workspace, key: step.key });
      if (step.kind === 'roles') {
        const result = await this.roles({ edit: step.edit, level: step.level, workspace: step.workspace });
        if (!result.error && !result.needs_user_words) {
          from.pop();
          const next = rolesUndo(result, step);
          if (next) to.push(next);
        }
        await this.reload();
        if (result.error || result.needs_user_words) this.report(step.key, /changed since|changed elsewhere/i.test(errorMessage(result))
          ? 'Changed elsewhere. Showing current value.' : errorMessage(result));
        return result;
      }
      const args = { key: step.key, level: step.previous.level, workspace: step.workspace,
        revision: step.revision };
      if (step.previous.present) args.value = cliValue(step.previous.value);
      const result = await (step.previous.present ? this.set(args) : this.unset(args));
      if (result.ok) {
        from.pop();
        // Rebase only the known causal predecessor. A fresh edit after an
        // outside change must never make older undo entries safe to overwrite.
        for (const entry of [...from, ...to]) {
          if (entry.file === step.file && entry.revision === step.beforeRevision) entry.revision = result.revision;
        }
        if (result.changed) to.push(this.remember(result, step, step.revision));
      }
      await this.reload();
      if (!result.ok) this.report(step.key, /changed since|changed elsewhere/i.test(errorMessage(result))
        ? 'Changed elsewhere. Showing current value.' : errorMessage(result));
      return result;
    });
  }
}
