// Pure pane models. Every capability and choice comes from agt's documents.
import { LEVELS, PROVIDERS, displayValue, labelFor } from './settings.js';
import { accountRow } from './rows.js';
import { decodeUsage } from './usage.js';
export const orderedRole = role => ['edit', 'full'].includes(role.access);
export const roleName = name => name.replaceAll('-', ' ');
export function roleChoices(provider) {
  return { models: (provider.choices?.models || []).filter(choice => choice.allowed).map(choice => ({
    label: choice.model ?? 'Default', value: choice.model, selected: choice.model === provider.model })),
  efforts: (provider.choices?.efforts || []).map(value => ({ label: value ?? 'Default', value, selected: value === provider.effort })) };
}
export function roleSource(role, level) {
  const sources = Object.entries(role.sources || {}).filter(([, source]) => source.level !== level && LEVELS[source.level]);
  const entry = sources.sort((a, b) => Object.keys(LEVELS).indexOf(b[1].level) - Object.keys(LEVELS).indexOf(a[1].level))[0];
  return entry ? { text: `${LEVELS[entry[1].level]} wins`, key: entry[0], value: entry[0] === 'providers' ? role.providers.map(p => p.provider).join(', ') : role[entry[0]], ...entry[1] } : null;
}
export function moveProvider(role, from, to) {
  if (!orderedRole(role) || from < 0 || to < 0 || from >= role.providers.length || to >= role.providers.length || from === to) return null;
  const providers = role.providers.map(p => p.provider);
  const [provider] = providers.splice(from, 1); providers.splice(to, 0, provider);
  return { edit: { kind: 'reorder', role: role.name, providers },
    announcement: `${role.providers[from].entry} moved ${to < from ? 'up' : 'down'} to position ${to + 1} of ${providers.length} in ${roleName(role.name)}.` };
}
export function rolesUndo(result, target) {
  if (result.error || result.needs_user_words || result.dry_run || !result.undo_from || !result.if_unchanged || !Object.keys(result.changes || {}).length) return null;
  return { ...target, kind: 'roles', pane: 'roles', key: target.key, file: result.file, level: result.level,
    edit: { kind: 'restore', undo_from: result.undo_from, if_unchanged: result.if_unchanged } };
}
export function accountModels(list, usage, now) {
  const providers = usage ? decodeUsage(usage, now).providers : [];
  return (list?.runtimes || []).flatMap(runtime => runtime.accounts.map((account, index) => {
    const provider = providers.find(p => p.runtime === runtime.runtime);
    const observed = provider?.accounts.find(a => a.name === account.name);
    const fallback = { ...account, windows: [], availability: !account.enabled ? 'off' : account.auth?.loggedIn === true ? 'ok' : account.auth?.loggedIn === false ? 'logged_out' : 'unknown',
      capacity: account.limits?.max_jobs, reason: account.auth?.error || account.auth?.detail || null };
    const model = accountRow(provider || { runtime: runtime.runtime, label: runtime.runtime }, observed || fallback, now);
    return { ...model, account, providerFirst: index === 0, reason: (observed || fallback).reason,
      toggle: account.can_toggle === true ? { kind: 'switch', label: `${PROVIDERS[runtime.runtime]} ${account.name}`, enabled: account.enabled }
        : { kind: 'badge', tabIndex: 0, text: account.name === 'primary' ? 'primary' : 'discovered', reason: account.toggle_reason || 'This account cannot be toggled' },
      name: account.name === 'primary' ? '' : account.name };
  }));
}
// A new input invalidates a successful name check synchronously. Old replies
// cannot enable Add account after a provider/name change or a closed sheet.
export class AccountNameCheck {
  constructor(check) { this.check = check; this.serial = 0; this.state = { valid: false, checking: false, message: '' }; }
  invalidate() { this.serial++; return this.state = { valid: false, checking: false, message: '' }; }
  async validate(runtime, name) {
    const serial = ++this.serial;
    this.state = { valid: false, checking: !!name.trim(), message: '' };
    if (!name.trim()) return this.state;
    try {
      const result = await this.check(runtime, name);
      if (serial !== this.serial) return null;
      return this.state = { valid: result.ok === true, checking: false,
        message: result.ok ? '' : result.error?.message || result.message || result.error || 'Choose another account name.' };
    } catch (error) {
      if (serial !== this.serial) return null;
      return this.state = { valid: false, checking: false, message: String(error.message || error) };
    }
  }
}
export const SHARE_LABELS = { 'share.mcp': 'MCP servers', 'share.hooks': 'Hooks', 'share.watcher': 'Sync in background',
  'share.push_on_save': 'Sync on save in Claude Code', 'share.notify': 'Notify about conflicts', 'share.instructions': 'Instructions',
  'share.auto_copy': 'New items', 'share.tool_widening': 'Tools in copies', 'share.worktrees': 'Worktrees', 'share.codex_session': 'Sync on session start in Codex',
  'share.enabled': 'Sharing', 'share.kinds': 'Shared kinds', 'share.plugins': 'Plugins' };
export const SHARE_ORDER = ['share.mcp', 'share.hooks', 'share.watcher', 'share.push_on_save', 'share.notify', 'share.instructions', 'share.auto_copy'];
export const SHARE_MORE = ['share.tool_widening', 'share.worktrees', 'share.codex_session'];
export const shellQuote = value => `'${String(value).replaceAll("'", "'\\''")}'`;
export function shareCell(item, label, cell) {
  const included = ['has', 'same', 'copy', 'ok', 'in_sync'].includes(cell.state);
  return { label, included, editable: cell.editable === true, reason: cell.reason || cell.note || cell.state,
    command: item.kind === 'instructions' ? `agt share adopt ${shellQuote(label)}`
      : `agt share pick ${shellQuote(item.id)} ${included ? '--not-in' : '--in'} ${shellQuote(label)}` };
}
export function sharingModel(list, status) {
  const enabled = status?.enabled === true && list?.enabled === true;
  return { enabled, state: enabled ? null : 'Sharing off', command: 'agt share enable --dry-run',
    waiting: (status?.attention || []).map(item => {
      const destination = (list?.folders || []).find(folder => !(item.folders || []).includes(folder.label)) || list?.folders?.[0];
      const command = (item.fix?.[0] || 'agt share status').replaceAll('<label>', destination ? shellQuote(destination.label) : '');
      return { ...item, command: command.includes('<') || !command.trim() ? 'agt share status' : command };
    }), items: (list?.items || []).map(item => ({ ...item,
      cells: Object.entries(item.cells).map(([label, cell]) => shareCell(item, label, cell)) })) };
}
export function shareResult(report) {
  for (const kind of ['mcp', 'hooks']) if (report[kind] === 'off') return `${SHARE_LABELS['share.' + kind]} off`;
  if (report.unchanged) return `${SHARE_LABELS['share.' + report.kind] || report.kind} ${report.enabled ? 'on' : 'off'}`;
  const shared = report.written?.length || 0, stays = (report.kept?.length || 0) + (report.waiting?.length || 0), failed = report.failed?.length || 0;
  return [`${shared} shared`, stays ? `${stays} stays` : '', failed ? `${failed} failed` : '',
    report.message || ''].filter(Boolean).join(' · ');
}
export function activityModel(entry, settings = [], options = {}) {
  const schema = settings.find(row => row.key === entry.key);
  const failed = entry.changes?.filter(change => change.ok === false).length || 0;
  const label = entry.type === 'setting' ? SHARE_LABELS[entry.key] || (schema ? labelFor(schema.key) : labelFor(entry.key || 'Setting'))
    : entry.type === 'sync' ? 'Sharing' : entry.message || 'Setting';
  const date = new Date(entry.time), time = Number.isNaN(date.getTime()) ? entry.time : date.toLocaleTimeString(options.locale, { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: options.timeZone });
  const value = value => value == null ? 'Not set' : Array.isArray(value) && value.some(item => item && typeof item === 'object') ? JSON.stringify(value) : displayValue(value);
  const change = entry.type === 'setting' ? `${value(entry.old)} → ${value(entry.new)}` : '';
  const marker = entry.type === 'refused' ? 'refused' : failed ? `failed ${failed}` : '';
  const tooltip = [entry.key, entry.by && `by ${entry.by}`, entry.file && `${entry.file}:${entry.line || 1}`, entry.message,
    ...(entry.changes || []).filter(c => !c.ok).map(c => c.message)].filter(Boolean).join('\n');
  return { entry, time, label, change, level: LEVELS[entry.level] || '', marker, tooltip,
    search: `${label} ${entry.key || ''} ${change} ${marker} ${tooltip} ${entry.type} ${entry.command || ''} ${entry.level || ''}`.toLowerCase() };
}
export const filterActivity = (entries, query, settings = [], options = {}) => entries.map(e => activityModel(e, settings, options)).filter(row => row.search.includes(query.trim().toLowerCase()));
