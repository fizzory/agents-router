import { act, fireEvent, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import broken from '../../../../tests/fixtures/app/config-show-broken.json';
import needsWords from '../../../../tests/fixtures/app/config-set-needs-user-words.json';
import { envelope, fixtures, nav, paneKeys, renderSettings, settingRow, settle } from '../test/settings';
import { SETTINGS_REFRESH_DELAY_MS } from './SettingsApp';

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
    expect(paneKeys()).toEqual(['max_jobs', 'max_depth', 'job_retention_days', 'subagents.prefer_roles', 'usage.statusline']);
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
    expect(unlistened.sort()).toEqual(['onAccountChecksStatus', 'onInstallStatus', 'onPaneMenu', 'onSettingsEdit', 'onSettingsHistory', 'onSettingsRefresh', 'onSettingsStatus',
      'onSettingsTarget', 'onUpdateStatus', 'onUsageUpdated']);
  });
});

describe('Settings refresh', () => {
  const readNames = ['configShow', 'shareStatus', 'shareList', 'rolesShow', 'accountsList', 'activityLog'] as const;
  type FakeApi = Awaited<ReturnType<typeof renderSettings>>['api'];
  const counts = (api: FakeApi) => Object.fromEntries(readNames.map(name => [name, api[name].mock.calls.length]));
  const clear = (api: FakeApi) => readNames.forEach(name => api[name].mockClear());
  const empty = { configShow: 0, shareStatus: 0, shareList: 0, rolesShow: 0, accountsList: 0, activityLog: 0 };

  it('mounts hidden without starting any agt reads', async () => {
    const { api } = await renderSettings({ visible: false, target: { pane: 'sharing' } });
    expect(counts(api)).toEqual(empty);
    expect(api.getSnapshot).toHaveBeenCalled();
    expect(api.getSettingsTarget).not.toHaveBeenCalled();
  });

  it('updates the status snapshot without reading config, panes or the badge', async () => {
    const { api, emit } = await renderSettings({ target: { pane: 'accounts' } });
    clear(api);
    await emit('onSettingsStatus', envelope(2));
    await emit('onSettingsStatus', envelope(3));
    expect(counts(api)).toEqual(empty);
  });

  it('a timer rereads a showing Sharing pane and, with a workspace selected, both settings documents', async () => {
    const { api, emit } = await renderSettings({ target: { pane: 'sharing', workspace: '/project' } });
    clear(api);
    await emit('onSettingsRefresh', { kinds: ['timer'] });
    expect(counts(api)).toEqual({ ...empty, configShow: 2, shareList: 1, shareStatus: 1 });
    expect(api.configShow).toHaveBeenCalledWith('/project');
    expect(api.configShow).toHaveBeenCalledWith(null);
  });

  it('rereads the User settings Sharing shows under a selected workspace, so its next write is not stale', async () => {
    const { api, emit } = await renderSettings({ target: { pane: 'sharing', workspace: '/project' } });
    const outside = structuredClone(fixtures.configShow);
    for (const row of outside.settings) if (row.levels.user) row.levels.user.revision = 'after-outside-edit';
    api.configShow.mockResolvedValue(outside);
    clear(api);
    await emit('onSettingsRefresh', { kinds: ['focus'] });
    // Both documents, and no share status for it.
    expect(counts(api)).toEqual({ ...empty, configShow: 2 });
    expect(api.configShow).toHaveBeenCalledWith(null);
    fireEvent.click(document.getElementById('setting-share-auto_copy')!);
    fireEvent.click(within(screen.getByRole('menu')).getByRole('menuitemcheckbox', { name: 'Agents' }));
    await settle();
    expect(api.configSet).toHaveBeenLastCalledWith(expect.objectContaining({ key: 'share.auto_copy', level: 'user', workspace: null,
      revision: 'after-outside-edit' }));
    // Off screen, the loaded Sharing pane's document still follows.
    fireEvent.click(nav('general')!);
    await settle();
    clear(api);
    await emit('onSettingsRefresh', { kinds: ['focus'] });
    expect(counts(api)).toEqual({ ...empty, configShow: 2 });
  });

  it.each([[null], ['/project']])('a share refresh rereads a showing Sharing pane and no settings document (workspace %j)', async (workspace) => {
    const { api, emit } = await renderSettings({ target: { pane: 'sharing', workspace } });
    clear(api);
    // A sync changes no settings file: the watched User file sends `config` for that.
    await emit('onSettingsRefresh', { kinds: ['share'] });
    expect(counts(api)).toEqual({ ...empty, shareList: 1, shareStatus: 1 });
    // The User file is still reread when a refresh asks for config.
    await emit('onSettingsRefresh', { kinds: ['share', 'config'] });
    expect(counts(api)).toEqual({ ...empty, configShow: workspace ? 2 : 1, shareList: 2, shareStatus: 2 });
    expect(api.configShow).toHaveBeenCalledWith(null);
  });

  it('refreshes the badge once and reloads dirty Sharing on selection', async () => {
    const { api, emit } = await renderSettings({ target: { pane: 'sharing' } });
    fireEvent.click(nav('general')!);
    clear(api);
    await emit('onSettingsRefresh', { kinds: ['share'] });
    expect(counts(api)).toEqual({ ...empty, shareStatus: 1 });
    fireEvent.click(nav('sharing')!);
    await settle();
    expect(counts(api)).toEqual({ ...empty, configShow: 1, shareStatus: 2, shareList: 1 });
  });

  it('a settings change and the timer refresh the Sharing badge, or the Sharing pane while it shows; a focus does not', async () => {
    const { api, emit } = await renderSettings();
    clear(api);
    await emit('onSettingsRefresh', { kinds: ['config'] });
    expect(counts(api)).toEqual({ ...empty, configShow: 1, shareStatus: 1 });
    await emit('onSettingsRefresh', { kinds: ['timer'] });
    expect(counts(api)).toEqual({ ...empty, configShow: 2, shareStatus: 2 });
    await emit('onSettingsRefresh', { kinds: ['focus'] });
    expect(counts(api)).toEqual({ ...empty, configShow: 3, shareStatus: 2 });
    fireEvent.click(nav('sharing')!);
    await settle();
    clear(api);
    await emit('onSettingsRefresh', { kinds: ['config'] });
    expect(counts(api)).toEqual({ ...empty, configShow: 1, shareList: 1, shareStatus: 1 });
    await emit('onSettingsRefresh', { kinds: ['timer'] });
    expect(counts(api)).toEqual({ ...empty, configShow: 2, shareList: 2, shareStatus: 2 });
    await emit('onSettingsRefresh', { kinds: ['focus'] });
    expect(counts(api)).toEqual({ ...empty, configShow: 3, shareList: 2, shareStatus: 2 });
  });

  it('merges repeated refreshes during a read into one trailing pass per target', async () => {
    const { api, emit } = await renderSettings({ target: { pane: 'roles' } });
    clear(api);
    const finish: ((value: unknown) => void)[] = [];
    const held = () => new Promise(resolve => { finish.push(resolve); });
    api.configShow.mockImplementationOnce(held);
    api.rolesShow.mockImplementationOnce(held);
    api.shareStatus.mockImplementationOnce(held);
    for (const kind of ['timer', 'focus', 'config', 'timer']) await emit('onSettingsRefresh', { kinds: [kind] });
    expect(counts(api)).toEqual({ ...empty, configShow: 1, rolesShow: 1, shareStatus: 1 });
    await act(async () => { finish[0]!(fixtures.configShow); finish[1]!(fixtures.roles); finish[2]!(fixtures.shareStatus); });
    await settle();
    expect(counts(api)).toEqual({ ...empty, configShow: 2, rolesShow: 2, shareStatus: 2 });
  });

  it('reloads a cached Accounts or Roles pane when selected after a settings change, a focus or an open it missed; the timer, Roles only', async () => {
    const { api, emit } = await renderSettings({ target: { pane: 'accounts' } });
    fireEvent.click(nav('roles')!);
    await settle();
    fireEvent.click(nav('general')!);
    await settle();
    clear(api);
    const added = structuredClone(fixtures.accountsList);
    const claude = added.runtimes[0]!.accounts as Record<string, unknown>[];
    claude.push({ ...claude[1]!, name: 'added-elsewhere' });
    api.accountsList.mockResolvedValue(added);
    const rows = () => document.querySelectorAll('.settings-account').length;
    const visit = async (pane: string) => {
      fireEvent.click(nav(pane)!);
      await settle();
      const shown = pane === 'accounts' ? rows() : 0;
      fireEvent.click(nav('general')!);
      await settle();
      return shown;
    };
    const listed = fixtures.accountsList.runtimes.reduce((count, runtime) => count + runtime.accounts.length, 0);
    for (const [round, event] of ([['onSettingsRefresh', { kinds: ['config'] }], ['onSettingsRefresh', { kinds: ['focus'] }],
      ['onSettingsTarget', { pane: 'general', open_id: 7 }]] as const).entries()) {
      await emit(event[0], event[1]);
      // Nothing is read for a pane that isn't showing.
      expect([api.accountsList.mock.calls.length, api.rolesShow.mock.calls.length]).toEqual([round, round]);
      expect(await visit('accounts')).toBe(listed + 1);
      await visit('roles');
      expect([api.accountsList.mock.calls.length, api.rolesShow.mock.calls.length]).toEqual([round + 1, round + 1]);
      // Read once: a second visit shows the cached pane.
      await visit('accounts');
      await visit('roles');
      expect([api.accountsList.mock.calls.length, api.rolesShow.mock.calls.length]).toEqual([round + 1, round + 1]);
    }
    // The timer reads neither, and marks only Roles: a workspace settings file can change unwatched.
    await emit('onSettingsRefresh', { kinds: ['timer'] });
    expect([api.accountsList.mock.calls.length, api.rolesShow.mock.calls.length]).toEqual([3, 3]);
    await visit('accounts');
    await visit('roles');
    expect([api.accountsList.mock.calls.length, api.rolesShow.mock.calls.length]).toEqual([3, 4]);
  });

  it('shows Roles a workspace file changed on the next selection after a timer, reading nothing before it', async () => {
    const { api, emit } = await renderSettings({ target: { pane: 'roles', workspace: '/w' } });
    fireEvent.click(nav('general')!);
    await settle();
    clear(api);
    // An agent edits /w/.agt/settings.toml, which no watcher sees.
    const edited = structuredClone(fixtures.roles);
    edited.roles.push({ ...edited.roles[0]!, name: 'added-in-workspace' });
    api.rolesShow.mockResolvedValue(edited);
    await emit('onSettingsRefresh', { kinds: ['timer'] });
    expect(api.rolesShow).not.toHaveBeenCalled();
    fireEvent.click(nav('roles')!);
    await settle();
    expect(api.rolesShow).toHaveBeenCalledTimes(1);
    expect(api.rolesShow).toHaveBeenCalledWith('/w');
    expect(document.querySelector('.role-row[data-role="added-in-workspace"]')).toBeTruthy();
  });

  it('keeps a refresh a write overtook and reads it when the write releases', async () => {
    const { api, emit } = await renderSettings();
    clear(api);
    let reply!: (visible: boolean) => void, written!: (value: unknown) => void;
    api.settingsVisible.mockImplementationOnce(() => new Promise(resolve => { reply = resolve; }));
    await emit('onSettingsRefresh', { kinds: ['share'] });
    // The badge's visibility check is out; a write starts before its reply.
    api.configSet.mockImplementationOnce(() => new Promise(resolve => { written = resolve; }));
    fireEvent.click(control('subagents.prefer_roles'));
    await settle();
    expect(api.configSet).toHaveBeenCalledTimes(1);
    await act(async () => { reply(true); });
    await settle();
    expect(api.shareStatus).not.toHaveBeenCalled();
    await act(async () => { written(fixtures.configSet); });
    await settle();
    expect(api.shareStatus).toHaveBeenCalledTimes(1);
  });

  it('a refresh that arrives during a visibility check gets its own answer, not the one already asked for', async () => {
    const { api, emit } = await renderSettings();
    clear(api);
    let reply!: (visible: boolean) => void;
    api.settingsVisible.mockImplementationOnce(() => new Promise(resolve => { reply = resolve; }));
    await emit('onSettingsRefresh', { kinds: ['focus'] });
    await emit('onSettingsRefresh', { kinds: ['focus'] });
    expect(api.configShow).not.toHaveBeenCalled();
    // The first check was asked while Settings was closing; it is open again for the second refresh.
    await act(async () => { reply(false); });
    await settle();
    expect(api.configShow).toHaveBeenCalledTimes(1);
  });

  it('one share status read serves the badge and the Sharing pane', async () => {
    const { api, emit } = await renderSettings();
    // As the app does: a second share status read answers the first "A newer read is running".
    const running: { finish: () => void; supersede: () => void; done: boolean }[] = [];
    api.shareStatus.mockImplementation(() => new Promise((resolve, reject) => {
      for (const older of running) if (!older.done) older.supersede();
      const read = { done: false, finish: () => { read.done = true; resolve(structuredClone(fixtures.shareStatus)); },
        supersede: () => { read.done = true; reject(new Error('A newer read is running')); } };
      running.push(read);
    }));
    const finishAll = async () => {
      for (let round = 0; round < 4; round += 1) {
        await act(async () => { for (const read of running) if (!read.done) read.finish(); });
        await settle();
      }
    };
    clear(api);
    fireEvent.click(nav('sharing')!);
    await settle();
    fireEvent.click(nav('general')!);
    await emit('onSettingsRefresh', { kinds: ['share'] });
    // The pane's read is still out: the badge waits for it instead of starting a second one.
    expect(api.shareStatus).toHaveBeenCalledTimes(1);
    await finishAll();
    expect(api.shareStatus).toHaveBeenCalledTimes(2);
    fireEvent.click(nav('sharing')!);
    await finishAll();
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
    expect(document.querySelectorAll('.share-item')).toHaveLength(fixtures.shareList.items.length);
    expect(nav('sharing')?.getAttribute('aria-label')).toBe('Sharing, needs attention');
  });

  it('starts no pass after unmount: not the one reserved behind a read, nor a visibility check for it', async () => {
    const { api, emit, unmount } = await renderSettings({ target: { pane: 'activity' } });
    clear(api);
    let finish!: (value: unknown) => void;
    api.activityLog.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    await emit('onSettingsRefresh', { kinds: ['activity'] });
    await emit('onSettingsRefresh', { kinds: ['activity'] });
    expect(api.activityLog).toHaveBeenCalledTimes(1);
    const checks = api.settingsVisible.mock.calls.length;
    unmount();
    await act(async () => { finish(fixtures.activity); });
    await settle();
    expect(api.activityLog).toHaveBeenCalledTimes(1);
    expect(api.settingsVisible.mock.calls.length).toBe(checks);
  });

  it('a settings read that finishes after unmount starts no read of its own', async () => {
    const { api, emit, unmount } = await renderSettings();
    clear(api);
    let finish!: (value: unknown) => void;
    api.configShow.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    await emit('onSettingsRefresh', { kinds: ['config'] });
    const before = counts(api);
    unmount();
    const changed = structuredClone(fixtures.configShow);
    changed.settings.find(row => row.key === 'share.enabled')!.value = !changed.settings.find(row => row.key === 'share.enabled')!.value;
    await act(async () => { finish(changed); });
    await settle();
    expect(counts(api)).toEqual(before);
  });

  it('a Retry reads at once; a pass queued behind it waits until Settings is visible', async () => {
    const { api, emit } = await renderSettings({ target: { pane: 'activity' } });
    api.activityLog.mockRejectedValueOnce(new Error('agt failed'));
    await emit('onSettingsRefresh', { kinds: ['activity'] });
    clear(api);
    // Settings is closing as Retry is clicked: the click's own read still runs.
    api.settingsVisible.mockResolvedValue(false);
    let finish!: (value: unknown) => void;
    api.activityLog.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await settle();
    expect(api.activityLog).toHaveBeenCalledTimes(1);
    // Show history asks for Activity again behind it.
    await emit('onSettingsHistory', { key: 'share.mcp' });
    await act(async () => { finish(fixtures.activity); });
    await settle();
    expect(api.activityLog).toHaveBeenCalledTimes(1);
    // Visible again: the pass that was dropped left Activity to reload when selected.
    api.settingsVisible.mockResolvedValue(true);
    fireEvent.click(nav('general')!);
    await settle();
    fireEvent.click(nav('activity')!);
    await settle();
    expect(api.activityLog).toHaveBeenCalledTimes(2);
  });

  it('catches up once when Settings opens after a hidden launch', async () => {
    const { api, emit } = await renderSettings({ visible: false });
    api.settingsVisible.mockResolvedValue(true);
    await emit('onSettingsTarget', { pane: 'accounts', open_id: 1 });
    expect(counts(api)).toEqual({ ...empty, configShow: 1, accountsList: 1, shareStatus: 1 });
    expect(document.body.dataset.pane).toBe('accounts');
  });

  it('loads Accounts on native open while WebKit is hidden and animation frames are paused', async () => {
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation(() => 1);
    const { api, dispatch } = await renderSettings({ visible: false });
    api.settingsVisible.mockResolvedValue(true);
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    try {
      await act(async () => {
        dispatch('onSettingsTarget', { pane: 'accounts', runtime: 'claude', account: 'work' });
        // The native smoke delivered focus 34 ms after the target.
        await vi.advanceTimersByTimeAsync(34);
        dispatch('onSettingsRefresh', { kinds: ['focus'] });
        await vi.advanceTimersByTimeAsync(SETTINGS_REFRESH_DELAY_MS);
      });
    } finally { vi.useRealTimers(); }
    expect(document.querySelector('.settings-account[data-key="claude:work"]')).toBeTruthy();
    expect(counts(api)).toEqual({ ...empty, configShow: 1, accountsList: 1, shareStatus: 1 });
  });

  it('coalesces a retained startup target and its late event but catches up on the next open', async () => {
    const target = { pane: 'sharing', open_id: 1 };
    const { api, emit } = await renderSettings({ target });
    await emit('onSettingsTarget', target);
    expect(counts(api)).toEqual({ ...empty, configShow: 1, shareList: 1, shareStatus: 1 });
    await emit('onSettingsTarget', { ...target, open_id: 2 });
    expect(counts(api)).toEqual({ ...empty, configShow: 2, shareList: 2, shareStatus: 2 });
  });

  it('drops a trailing read when hidden and reads the dirty pane on selection', async () => {
    const { api, emit } = await renderSettings({ target: { pane: 'activity' } });
    clear(api);
    let finish!: (value: unknown) => void;
    api.activityLog.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    await emit('onSettingsRefresh', { kinds: ['activity'] });
    await emit('onSettingsRefresh', { kinds: ['activity'] });
    expect(api.activityLog).toHaveBeenCalledTimes(1);
    api.settingsVisible.mockResolvedValue(false);
    await act(async () => { finish(fixtures.activity); });
    await settle();
    expect(api.activityLog).toHaveBeenCalledTimes(1);
    // Visible again with no open event, which would mark Activity itself: only
    // the mark the dropped pass left can make the selection read.
    api.settingsVisible.mockResolvedValue(true);
    fireEvent.click(nav('general')!);
    await settle();
    fireEvent.click(nav('activity')!);
    await settle();
    expect(api.activityLog).toHaveBeenCalledTimes(2);
  });

  it('reloads a showing pane whose trailing pass was dropped while hidden on the focus that restores Settings', async () => {
    const { api, emit } = await renderSettings({ target: { pane: 'sharing' } });
    clear(api);
    let finish!: (value: unknown) => void;
    api.shareList.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const synced = { ...structuredClone(fixtures.shareList), items: fixtures.shareList.items.slice(1) };
    api.shareList.mockResolvedValue(synced);
    await emit('onSettingsRefresh', { kinds: ['share'] });
    // A second sync during the read reserves a pass; the person minimizes before the read ends.
    await emit('onSettingsRefresh', { kinds: ['share'] });
    expect(api.shareList).toHaveBeenCalledTimes(1);
    api.settingsVisible.mockResolvedValue(false);
    await act(async () => { finish(fixtures.shareList); });
    await settle();
    expect(api.shareList).toHaveBeenCalledTimes(1);
    expect(document.querySelectorAll('.share-item')).toHaveLength(fixtures.shareList.items.length);
    // The app sent `share` already and kept nothing: only the dropped pass's mark covers Sharing on this focus.
    api.settingsVisible.mockResolvedValue(true);
    clear(api);
    await emit('onSettingsRefresh', { kinds: ['focus'] });
    expect(counts(api)).toEqual({ ...empty, configShow: 1, shareList: 1, shareStatus: 1 });
    expect(document.querySelectorAll('.share-item')).toHaveLength(synced.items.length);
    // Read once: the next focus leaves Sharing alone.
    await emit('onSettingsRefresh', { kinds: ['focus'] });
    expect(counts(api)).toEqual({ ...empty, configShow: 2, shareList: 1, shareStatus: 1 });
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
    expect(paneKeys()).toHaveLength(5);
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
    await emit('onSettingsRefresh', { kinds: ['timer'] });
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
    await emit('onSettingsRefresh', { kinds: ['timer'] });
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
