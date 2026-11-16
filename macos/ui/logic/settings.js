// Values, provenance and navigation derived only from agt's schema-1 document.
export const LEVELS = { user: 'User', workspace: 'Workspace', local: 'Local' };
export const PROVIDERS = { claude: 'Claude', codex: 'Codex', antigravity: 'Antigravity', kiro: 'Kiro' };
export const CONTROL_KINDS = { int: 'stepper', float: 'stepper', bool: 'switch', choice: 'popup', text: 'field', names: 'field', auto_copy: 'new-items', fixed: 'file' };
export const controlFor = (kind) => CONTROL_KINDS[kind];
export function labelFor(key) {
  const names = { 'subagents.prefer_roles': 'Prefer roles', job_retention_days: 'Keep jobs for days', primary: 'Main account folder', binary: 'CLI path', usage_ceiling: 'Usage ceiling', primary_models: 'Main account models', primary_max_jobs: 'Main account max jobs', primary_max_effort: 'Main account max effort' };
  const word = key.includes('.') ? key.split('.').slice(1).join('.') : key;
  const text = names[key] || names[word] || word.replaceAll('_', ' ');
  return text.charAt(0).toUpperCase() + text.slice(1);
}
// Panes: General, Roles, Accounts, Sharing, Activity, then one per provider.
export const PANES = { general: 'General', roles: 'Roles', accounts: 'Accounts', sharing: 'Sharing', activity: 'Activity',
  claude: 'Claude', codex: 'Codex', antigravity: 'Antigravity', kiro: 'Kiro' };
// A key's pane from its prefix: provider keys go to that provider, share.* to
// Sharing, roles.* to Roles, everything else (subagents.prefer_roles too) to General.
export function paneForKey(key = '') {
  const [prefix, ...rest] = key.split('.');
  if (!rest.length) return 'general';
  return PROVIDERS[prefix] ? prefix : prefix === 'share' ? 'sharing' : prefix === 'roles' ? 'roles' : 'general';
}
export const paneFor = (row) => paneForKey(row.key);
// Provider pane groups by key suffix. A frontend constant: unknown keys go to Other.
export const GROUPS = {
  Accounts: ['primary', 'binary', 'accounts', 'include_primary', 'order', 'ignore'],
  Limits: ['max_jobs', 'max_jobs_per_account', 'primary_max_jobs', 'usage_ceiling', 'when_full', 'failover'],
  Models: ['plan', 'default_model', 'primary_models', 'primary_max_effort', 'edit_permission_mode', 'connectors'],
  'Retries and health': ['cooldown_minutes', 'capacity_retries', 'capacity_retry_seconds'],
  Runtime: ['share_parent_mcp', 'network_access', 'web_search', 'windows_sandbox', 'reasoning_summary', 'reviewer', 'reviewer_model', 'reviewer_account', 'sandbox', 'engine'],
};
export const GROUP_ORDER = [...Object.keys(GROUPS), 'Other'];
export const FILE_ONLY = 'File only';
export function groupFor(row) {
  if (row.kind === 'fixed') return FILE_ONLY;
  if (paneFor(row) === 'general') return 'Jobs';
  const suffix = row.key.split('.').slice(1).join('.');
  if (suffix.startsWith('circuit_')) return 'Retries and health';
  return Object.keys(GROUPS).find(group => GROUPS[group].includes(suffix)) || 'Other';
}
export const displayValue = (value) => value == null ? '' : Array.isArray(value) ? value.join(', ') : typeof value === 'object' ? JSON.stringify(value) : String(value);
export function cliValue(value) {
  return Array.isArray(value) ? value.length ? value.join(',') : '[]' : String(value);
}
export function rowState(row, level, document = {}) {
  const accepted = row.accepted_levels || Object.keys(LEVELS);
  const allowed = accepted.includes(level);
  const shownLevel = allowed ? level : accepted.includes('user') ? 'user' : accepted[0];
  const own = row.levels[shownLevel];
  const value = row.kind === 'fixed' ? row.value : own?.present ? own.value : row.source === 'default' ? row.value : null;
  const winning = row.source !== 'default' && row.source !== level;
  const sourceLabel = LEVELS[row.source] || (row.source.startsWith('environment') ? 'Environment' : row.source);
  const chip = !allowed ? `${LEVELS[shownLevel]} only` : winning ? `${sourceLabel} wins` : null;
  const winner = row.levels[row.source];
  const location = winner?.file || row.file;
  const line = winner?.line || row.line || 1;
  return { value, own, level: shownLevel, editable: row.editable && allowed && !document.error,
    reset: allowed && !!own?.present && !chip, chip, chipLevel: !allowed ? shownLevel : LEVELS[row.source] ? row.source : null,
    chipTooltip: chip ? `${displayValue(winner?.present ? winner.value : row.value)} — ${location ? `${location}:${line}` : row.source}` : '',
    label: `${PROVIDERS[row.key.split('.')[0]] || ''} ${labelFor(row.key)}`.trim(),
    tooltip: `${row.key}\n${row.description}\n${row.applies}${row.still_limited_by?.length ? '\n' + row.still_limited_by.join('\n') : ''}` };
}
export function matches(row, query) {
  return `${PROVIDERS[row.key.split('.')[0]] || ''} ${labelFor(row.key)} ${row.key} ${row.description}`.toLowerCase().includes(query.trim().toLowerCase());
}
export function paneRows(document, pane, query = '') {
  return document.settings.filter(row => paneFor(row) === pane && matches(row, query));
}
export function fileTarget(row, level) {
  const own = row.levels[level];
  return { file: row.file || own.file, line: row.line || own.line || 1 };
}
export function brokenFile(document) {
  if (!document.error) return null;
  const message = errorMessage(document.error);
  const entry = document.files.find(item => message.includes(item.file)) || document.files[0];
  const line = Number(message.match(/(?:at line |line |:)(\d+)/i)?.[1]) || 1;
  return { message, file: entry?.file, line };
}
export function errorMessage(result) {
  const error = result?.error ?? result;
  if (typeof error === 'string') return result?.ask || error;
  return error?.message || error?.ask || result?.ask || String(error);
}
export function installState(snapshot = {}) {
  const error = snapshot.error || '';
  const problem = /not found/i.test(error) ? { message: "agt isn't installed.", action: 'Install' }
    : /too old|unsupported.*schema/i.test(error) ? { message: /[.!]$/.test(error) ? error : `${error}.`, action: 'Repair' } : null;
  const blockers = snapshot.usage?.uninstall_blockers;
  const count = blockers?.length ?? null;
  return { problem, version: snapshot.usage?.agt_version || error.match(/\bagt\s+(\d+\.\d+(?:\.\d+)?)/)?.[1] || null,
    owned: snapshot.usage?.install?.source === 'app', uninstallDisabled: count !== 0,
    uninstallReason: count == null ? 'Checking running jobs…' : count ? `Wait for ${count} running job${count === 1 ? '' : 's'} to finish.` : '' };
}
export const isTextField = (element) => !!element?.matches?.('input:not([type=checkbox]):not([type=button]), textarea, [contenteditable=true]');
export function dispatchEdit(action, { activeElement, editField, history }) {
  return isTextField(activeElement) ? editField(action) : history[action]();
}

// A field owns its draft until blur/Enter; a duplicate blur cannot add a step.
export function fieldCommit(initial, commit) {
  let baseline = initial;
  return value => {
    if (value === baseline) return false;
    baseline = value;
    commit(value);
    return true;
  };
}
