import { act, fireEvent, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import broken from '../../../../tests/fixtures/app/config-show-broken.json';
import needsWords from '../../../../tests/fixtures/app/config-set-needs-user-words.json';
import { fixtures, nav, paneKeys, renderSettings, settingRow, settle } from '../test/settings';

const control = (key: string) => document.getElementById(`setting-${key.replaceAll('.', '-')}`) as HTMLInputElement;
const announced = () => document.getElementById('announcement')?.textContent?.replace('​', '');
const userRevision = (key: string) => fixtures.configShow.settings.find((row) => row.key === key)?.levels.user?.revision ?? null;
async function commit(key: string, text: string) {
  const input = control(key);
  fireEvent.focus(input);
  fireEvent.change(input, { target: { value: text } });
  fireEvent.keyDown(input, { key: 'Enter' });
  fireEvent.blur(input);
  await settle();
}

describe('sidebar and panes', () => {
  it('renders only the selected pane; every config key appears once across the nine panes', async () => {
    await renderSettings();
    expect(document.body.dataset.pane).toBe('general');
    expect(paneKeys()).toEqual(['max_jobs', 'max_depth', 'job_retention_days', 'subagents.prefer_roles']);
    const seen: string[] = [...paneKeys() as string[]];
    for (const pane of ['claude', 'codex', 'antigravity', 'kiro', 'sharing', 'roles', 'accounts', 'activity']) {
      fireEvent.click(nav(pane)!);
      await settle();
      expect(nav(pane)?.getAttribute('aria-current')).toBe('page');
      expect(document.querySelectorAll('nav [aria-current]')).toHaveLength(1);
      expect(document.body.dataset.pane).toBe(pane);
      seen.push(...paneKeys() as string[]);
    }
    expect(seen.sort()).toEqual(fixtures.configShow.settings.map((row) => row.key).sort());
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Activity');
  });

  it('shows attention dots in the accessible name of General and Sharing', async () => {
    const { emit } = await renderSettings();
    expect(nav('sharing')?.getAttribute('aria-label')).toBe('Sharing, needs attention');
    expect(nav('general')?.getAttribute('aria-label')).toBe('General');
    await emit('onSettingsStatus', { usage: null, error: 'agt not found', reading: false, generation: 2, update_version: null });
    expect(nav('general')?.getAttribute('aria-label')).toBe('General, needs attention');
    expect(screen.getByText("agt isn't installed.")).toBeTruthy();
  });

  it('navigates to a provider pane by key prefix and highlights the row', async () => {
    const { emit } = await renderSettings();
    await emit('onSettingsTarget', { key: 'claude.max_jobs', level: 'user' });
    expect(document.body.dataset.pane).toBe('claude');
    await act(async () => { await new Promise((resolve) => requestAnimationFrame(resolve)); });
    expect(document.activeElement).toBe(settingRow('claude.max_jobs'));
    expect(settingRow('claude.max_jobs')?.classList.contains('setting-flash')).toBe(true);
    await emit('onSettingsTarget', { pane: 'accounts', runtime: 'claude', account: 'work' });
    expect(document.body.dataset.pane).toBe('accounts');
    expect(document.activeElement?.getAttribute('data-key')).toBe('claude:work');
  });

  it('unsubscribes every event on unmount', async () => {
    const { unmount, unlistened } = await renderSettings();
    unmount();
    expect(unlistened.sort()).toEqual(['onAccountChecksStatus', 'onInstallStatus', 'onPaneMenu', 'onSettingsEdit', 'onSettingsHistory', 'onSettingsStatus',
      'onSettingsTarget', 'onUpdateStatus', 'onUsageUpdated']);
  });
});

describe('search', () => {
  it('filters the selected pane, counts the others and says when nothing matches', async () => {
    const { api } = await renderSettings();
    const search = screen.getByRole('searchbox', { name: 'Search settings' });
    fireEvent.change(search, { target: { value: 'max jobs' } });
    await settle();
    expect(paneKeys()).toEqual(['max_jobs']);
    expect(settingRow('max_jobs')?.querySelector('mark')?.textContent?.toLowerCase()).toBe('max jobs');
    expect(nav('claude')?.getAttribute('aria-label')).toMatch(/^Claude, \d+ results?$/);
    expect(nav('roles')?.getAttribute('aria-label')).toBe('Roles, no results');
    for (const call of [api.rolesShow, api.accountsList, api.shareList, api.activityLog]) expect(call).toHaveBeenCalledTimes(1);
    fireEvent.change(search, { target: { value: 'zzzz' } });
    await settle();
    expect(screen.getByText('No results for “zzzz” in General.')).toBeTruthy();
    fireEvent.keyDown(search, { key: 'Escape' });
    await settle();
    expect((search as HTMLInputElement).value).toBe('');
    expect(paneKeys()).toHaveLength(4);
    for (const call of [api.rolesShow, api.accountsList, api.shareList, api.activityLog]) expect(call).toHaveBeenCalledTimes(1);
  });

  it('Show history opens Activity with the key in Search', async () => {
    const { emit } = await renderSettings();
    await emit('onSettingsHistory', { key: 'share.mcp' });
    expect(document.body.dataset.pane).toBe('activity');
    expect((screen.getByRole('searchbox') as HTMLInputElement).value).toBe('share.mcp');
    expect(document.body.dataset.historyKey).toBe('share.mcp');
  });
});

describe('scope popup', () => {
  it('Choose Folder… cancelled changes nothing; a chosen folder reads its settings', async () => {
    const { api } = await renderSettings();
    const scope = () => document.getElementById('scope') as HTMLButtonElement;
    expect(scope().textContent).toBe('User');
    fireEvent.click(scope());
    const menu = screen.getByRole('menu', { name: 'Settings scope' });
    expect(within(menu).getByRole('menuitemradio', { name: 'User' }).getAttribute('aria-checked')).toBe('true');
    fireEvent.click(within(menu).getByRole('menuitem', { name: 'Choose Folder…' }));
    await settle();
    expect(scope().textContent).toBe('User');
    expect(api.configShow).not.toHaveBeenCalledWith('/work/repo');
    api.chooseWorkspace.mockResolvedValueOnce('/work/repo');
    fireEvent.click(scope());
    fireEvent.click(screen.getByRole('menuitem', { name: 'Choose Folder…' }));
    await settle();
    expect(scope().textContent).toBe('repo: Workspace');
    expect(scope().getAttribute('data-tooltip')).toBe('/work/repo');
    expect(api.configShow).toHaveBeenCalledWith('/work/repo');
    fireEvent.click(scope());
    expect(screen.getByRole('menu').textContent).toContain('repo');
    expect(screen.getAllByRole('menuitemradio').map((item) => item.getAttribute('data-label'))).toEqual(['User', 'Workspace', 'Local']);
  });
});

describe('controls by kind', () => {
  it('number fields commit once on Enter, restore on Escape and clamp the stepper', async () => {
    const { api } = await renderSettings();
    await commit('max_jobs', '9');
    expect(api.configSet).toHaveBeenCalledTimes(1);
    expect(api.configSet).toHaveBeenCalledWith({ key: 'max_jobs', value: '9', level: 'user', workspace: null, revision: userRevision('max_jobs') });
    const input = control('max_jobs');
    expect(input.getAttribute('role')).toBe('spinbutton');
    expect(input.getAttribute('aria-valuemin')).toBe('1');
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: '12' } });
    fireEvent.keyDown(input, { key: 'Escape' });
    fireEvent.blur(input);
    await settle();
    expect(input.value).toBe('6');
    expect(api.configSet).toHaveBeenCalledTimes(1);
    const depth = control('max_depth');
    fireEvent.focus(depth);
    fireEvent.keyDown(depth, { key: 'ArrowDown' });
    fireEvent.keyDown(depth, { key: 'ArrowDown' });
    expect(depth.value).toBe('0');
    fireEvent.keyDown(depth, { key: 'ArrowUp' });
    expect(depth.value).toBe('1');
    fireEvent.change(depth, { target: { value: '-4' } });
    fireEvent.blur(depth);
    await settle();
    expect(api.configSet).toHaveBeenLastCalledWith(expect.objectContaining({ key: 'max_depth', value: '0' }));
  });

  it('switches, popups, text and names fields write CLI values', async () => {
    const { api } = await renderSettings();
    const prefer = control('subagents.prefer_roles');
    expect(prefer.getAttribute('role')).toBe('switch');
    expect(prefer.getAttribute('aria-checked')).toBe('true');
    fireEvent.click(prefer);
    await settle();
    expect(api.configSet).toHaveBeenLastCalledWith(expect.objectContaining({ key: 'subagents.prefer_roles', value: 'false' }));
    fireEvent.click(nav('claude')!);
    await settle();
    fireEvent.click(control('claude.failover'));
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'off' }));
    await settle();
    expect(api.configSet).toHaveBeenLastCalledWith(expect.objectContaining({ key: 'claude.failover', value: 'off' }));
    await commit('claude.order', 'work, lab');
    expect(api.configSet).toHaveBeenLastCalledWith(expect.objectContaining({ key: 'claude.order', value: 'work, lab' }));
    const plan = control('claude.plan');
    fireEvent.focus(plan);
    fireEvent.change(plan, { target: { value: 'max' } });
    fireEvent.blur(plan);
    await settle();
    expect(api.configSet).toHaveBeenLastCalledWith(expect.objectContaining({ key: 'claude.plan', value: 'max' }));
    expect(settingRow('claude.max_jobs_by_room')?.closest('details')?.querySelector('summary')?.textContent).toBe('File only (1)');
  });

  it('a focused field keeps its draft when a refresh lands', async () => {
    const { emit } = await renderSettings();
    const input = control('max_jobs');
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: '42' } });
    await emit('onSettingsStatus', { usage: null, error: null, reading: false, generation: 3, update_version: null });
    expect(input.value).toBe('42');
  });
});

describe('guarded writes', () => {
  it('a field focused without an edit keeps no old revision for later writes', async () => {
    const { api } = await renderSettings();
    fireEvent.focus(control('max_jobs'));
    fireEvent.blur(control('max_jobs'));
    const after = structuredClone(fixtures.configShow) as unknown as { settings: { levels: Record<string, Record<string, unknown> | undefined> }[] };
    for (const row of after.settings) if (row.levels.user) row.levels.user.revision = 'after-max-depth';
    api.configShow.mockResolvedValue(after);
    await commit('max_depth', '3');
    fireEvent.click(within(settingRow('max_jobs')!).getByRole('button', { name: 'Reset Max jobs' }));
    await settle();
    expect(api.configUnset).toHaveBeenLastCalledWith(expect.objectContaining({ key: 'max_jobs', revision: 'after-max-depth' }));
    expect(settingRow('max_jobs')?.querySelector('.inline-message')).toBeNull();
  });

  it('a draft saves against the revision it was typed on, so an outside edit is refused as stale', async () => {
    const { api, emit } = await renderSettings();
    const input = control('max_jobs');
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: '42' } });
    const outside = structuredClone(fixtures.configShow) as unknown as { settings: { key: string; levels: Record<string, Record<string, unknown>> }[] };
    const row = outside.settings.find((item) => item.key === 'max_jobs')!;
    row.levels.user = { ...row.levels.user, value: 99, revision: 'after-outside-edit' };
    api.configShow.mockResolvedValue(outside);
    await emit('onSettingsStatus', { usage: null, error: null, reading: false, generation: 3, update_version: null });
    expect(api.configShow).toHaveBeenCalledTimes(2);
    expect(input.value).toBe('42');
    api.configSet.mockResolvedValueOnce({ ok: false, error: { message: 'settings.toml changed since it was read' } });
    fireEvent.blur(input);
    await settle();
    expect(api.configSet).toHaveBeenCalledWith(expect.objectContaining({ key: 'max_jobs', value: '42', revision: userRevision('max_jobs') }));
    expect(settingRow('max_jobs')?.querySelector('.inline-message.is-stale')?.textContent).toBe('Changed elsewhere. Showing the current value.');
  });

  it('shows refusals, stale revisions and agt’s ask under the row', async () => {
    const { api } = await renderSettings();
    api.configSet.mockResolvedValueOnce({ ok: false, error: { message: "max_jobs takes a number, not 'x'" } });
    await commit('max_jobs', '7');
    const message = within(settingRow('max_jobs')!).getByRole('alert');
    expect(message.textContent).toBe("max_jobs takes a number, not 'x'");
    expect(control('max_jobs').getAttribute('aria-invalid')).toBe('true');
    expect(control('max_jobs').getAttribute('aria-errormessage')).toBe(message.id);
    api.configSet.mockResolvedValueOnce({ ok: false, error: { message: 'settings.toml changed since it was read' } });
    await commit('max_jobs', '8');
    expect(settingRow('max_jobs')?.querySelector('.inline-message.is-stale')?.textContent).toBe('Changed elsewhere. Showing the current value.');
    api.configSet.mockResolvedValueOnce({ ok: false, exit_code: 3, error: needsWords });
    await commit('max_depth', '3');
    expect(settingRow('max_depth')?.querySelector('.inline-message.is-warn')?.textContent).toBe(needsWords.ask);
    expect(settingRow('max_depth')?.querySelector('input:not([role])')).toBeNull();
  });

  it('a successful write rereads settings and joins history; undo and redo announce and navigate', async () => {
    const { api, emit } = await renderSettings();
    const reads = api.configShow.mock.calls.length;
    await commit('max_depth', '3');
    expect(api.configShow.mock.calls.length).toBeGreaterThan(reads);
    fireEvent.click(nav('claude')!);
    await settle();
    api.configUnset.mockResolvedValueOnce({ ok: true, changed: true, key: 'max_depth', file: '/agt/settings.toml', revision: 'after-undo',
      previous: { level: 'user', present: true, value: 3 } });
    await emit('onSettingsEdit', { action: 'undo' });
    expect(document.body.dataset.pane).toBe('general');
    expect(api.configUnset).toHaveBeenCalledWith(expect.objectContaining({ key: 'max_depth', level: 'user', revision: fixtures.configSet.revision }));
    expect(announced()).toBe('Undid Max depth');
    api.configSet.mockResolvedValueOnce({ ok: false, error: { message: 'settings.toml changed since it was read' } });
    await emit('onSettingsEdit', { action: 'redo' });
    expect(api.configSet).toHaveBeenLastCalledWith(expect.objectContaining({ key: 'max_depth', value: '3', revision: 'after-undo' }));
    expect(settingRow('max_depth')?.querySelector('.inline-message.is-stale')).toBeTruthy();
    api.configSet.mockClear();
    await emit('onSettingsEdit', { action: 'redo' });
    expect(api.configSet).toHaveBeenCalledWith(expect.objectContaining({ key: 'max_depth', value: '3', revision: 'after-undo' }));
    expect(announced()).toBe('Redid Max depth');
  });
});

describe('accessible names', () => {
  it('every control in every pane has an accessible name', async () => {
    await renderSettings();
    for (const pane of ['general', 'roles', 'accounts', 'sharing', 'activity', 'claude', 'codex', 'antigravity', 'kiro']) {
      fireEvent.click(nav(pane)!);
      await settle();
      const host = within(document.getElementById('pane-content')!);
      for (const role of ['switch', 'button', 'textbox', 'spinbutton', 'checkbox', 'combobox'] as const) {
        const all = host.queryAllByRole(role);
        const named = host.queryAllByRole(role, { name: /\S/ });
        expect(all.filter((node) => !named.includes(node)).map((node) => `${pane} ${role} ${node.outerHTML.slice(0, 120)}`)).toEqual([]);
      }
    }
  });
});

describe('broken settings file', () => {
  it('shows the line, agt’s diagnostic and Open settings.toml, and locks every row', async () => {
    const { api } = await renderSettings({ config: broken });
    const banner = screen.getByText(/settings\.toml has an error on line 2\./).closest('.banner') as HTMLElement;
    expect(within(banner).getByText(broken.error as string)).toBeTruthy();
    fireEvent.click(within(banner).getByRole('button', { name: 'Open settings.toml' }));
    expect(api.openSettingsFile).toHaveBeenCalledWith('<ROOT>/agt/settings.toml', 2);
    expect(control('max_jobs').readOnly).toBe(true);
    expect(within(settingRow('max_jobs')!).getByRole('img', { name: 'Locked: settings.toml has an error' })).toBeTruthy();
    await commit('max_jobs', '9');
    expect(api.configSet).not.toHaveBeenCalled();
  });
});
