import { act, fireEvent, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import statusOff from '../../../../tests/fixtures/app/share-status-off.json';
import { cssTime } from '../shared/motion';
import { media } from '../test/dom';
import { envelope, fixtures, nav, NOW, renderSettings, settingRow, settle } from '../test/settings';

async function open(pane: string, options = {}) {
  const view = await renderSettings(options);
  fireEvent.click(nav(pane)!);
  await settle();
  return view;
}
const announced = () => document.getElementById('announcement')?.textContent?.replace('​', '');
const chip = (role: string, entry: string) => document.querySelector<HTMLButtonElement>(`.provider-chip[data-role="${role}"][data-entry="${entry}"]`);

describe('Roles', () => {
  it('numbers ordered roles only and reorders with ⌥→, announcing the move', async () => {
    const { api } = await open('roles');
    expect(document.querySelectorAll('.role-row')).toHaveLength(3);
    expect(chip('reviewer', 'claude:work')?.querySelector('.chip-order')).toBeNull();
    const first = chip('builder', 'codex')!;
    expect(first.getAttribute('aria-label')).toBe('codex, position 1 of 2');
    expect(first.tabIndex).toBe(0);
    expect(chip('builder', 'claude:work')?.tabIndex).toBe(-1);
    fireEvent.keyDown(first, { key: 'ArrowRight' });
    expect(document.activeElement).toBe(chip('builder', 'claude:work'));
    fireEvent.keyDown(first, { key: 'ArrowRight', altKey: true });
    await settle();
    expect(api.rolesConfig).toHaveBeenCalledWith({ edit: { kind: 'reorder', role: 'builder', providers: ['claude:work', 'codex'] },
      level: 'user', workspace: null });
    expect(announced()).toMatch(/^codex moved down to position 2 of 2 in builder\.$/);
  });

  it('a chip opens the model and effort menu for every role; a role switch writes enable', async () => {
    const { api } = await open('roles');
    fireEvent.click(chip('builder', 'codex')!);
    const menu = screen.getByRole('menu');
    expect(menu.textContent).toContain('Applies to every role');
    const radios = within(menu).getAllByRole('menuitemradio');
    expect(radios.length).toBeGreaterThan(1);
    fireEvent.click(radios[0]!);
    await settle();
    expect(api.rolesConfig).toHaveBeenLastCalledWith(expect.objectContaining({ edit: expect.objectContaining({ entry: 'codex' }) }));
    const enabled = within(document.querySelector('[data-role="builder"]')!).getByRole('switch');
    fireEvent.click(enabled);
    await settle();
    expect(api.rolesConfig).toHaveBeenLastCalledWith(expect.objectContaining({ edit: { kind: 'enable', role: 'builder', enabled: false } }));
  });

  it('native menu moves send fixed action names and run on the matching token', async () => {
    const { api, emit } = await open('roles');
    fireEvent.contextMenu(chip('executor', 'claude:work')!);
    expect(api.showPaneMenu).toHaveBeenCalledWith('1', ['move-front', 'move-up']);
    await emit('onPaneMenu', { token: '1', action: 'move-front' });
    expect(api.rolesConfig).toHaveBeenCalledWith(expect.objectContaining({ edit: { kind: 'reorder', role: 'executor', providers: ['claude:work', 'codex'] } }));
    api.rolesConfig.mockClear();
    await emit('onPaneMenu', { token: '1', action: 'move-front' });
    expect(api.rolesConfig).not.toHaveBeenCalled();
  });
});

describe('Accounts', () => {
  it('keeps Settings copy failures on its existing report path', async () => {
    const { api, emit } = await open('accounts');
    const target = document.querySelector<HTMLElement>('.settings-account[data-key="claude:work"]')!;
    api.accountsLogin.mockResolvedValueOnce({ status: 'terminal_failed', copy_command: 'Rust login fallback' });
    fireEvent.click(within(target).getByRole('button', { name: 'More for Claude work' }));
    const [token] = api.showPaneMenu.mock.calls.at(-1) as [string];
    await emit('onPaneMenu', { token, action: 'login' });
    api.copyText.mockRejectedValueOnce(new Error('Settings copy failed'));
    const copy = within(target).getByRole('button', { name: 'Copy command' });
    fireEvent.click(copy);
    await settle();
    expect(copy.textContent).toBe('Copy command');
    expect(document.getElementById('status')?.textContent).toBe('Settings copy failed');
    expect(announced()).not.toBe("Couldn't copy.");
  });
  it('shares structured login recovery for both the row action and native menu', async () => {
    const { api, emit } = await open('accounts');
    const target = document.querySelector<HTMLElement>('.settings-account[data-key="claude:work"]')!;
    api.accountsLogin.mockResolvedValueOnce({ status: 'terminal_failed', copy_command: 'Rust login fallback' });
    fireEvent.click(within(target).getByRole('button', { name: 'More for Claude work' }));
    const [token] = api.showPaneMenu.mock.calls.at(-1) as [string];
    await emit('onPaneMenu', { token, action: 'login' });
    expect(api.accountsLogin).toHaveBeenCalledWith('claude', 'work');
    expect(within(target).getByText("Couldn't open Terminal.")).toBeTruthy();
    fireEvent.click(within(target).getByRole('button', { name: 'Copy command' }));
    await settle();
    expect(api.copyText).toHaveBeenCalledWith('Rust login fallback');
    expect(within(target).getByRole('button', { name: 'Copied' })).toBeTruthy();
    expect(within(target).getByRole('button', { name: 'Copied' }).textContent).toBe('Copied');
    expect(announced()).toBe('Copied');
    // A logged-out row uses the same result path without changing its layout.
    const updated = structuredClone(fixtures.accountsList);
    Object.assign(updated.runtimes[0]!.accounts[1]!, { auth: { loggedIn: false } });
    api.accountsList.mockResolvedValue(updated);
    await emit('onSettingsStatus', envelope(2));
    const out = [...document.querySelectorAll<HTMLElement>('.settings-account')].find(item =>
      item.querySelector('.row-action')?.textContent === 'Log in…')!;
    api.accountsLogin.mockResolvedValueOnce({ status: 'terminal_failed', copy_command: 'Row fallback' });
    fireEvent.click(out.querySelector('.row-action')!);
    await settle();
    fireEvent.click(within(out).getByRole('button', { name: 'Copy command' }));
    await settle();
    expect(api.copyText).toHaveBeenLastCalledWith('Row fallback');
    expect(within(out).getByRole('button', { name: 'Copied' }).textContent).toBe('Copied');
  });

  it('shows the gauge row, badges for untogglable accounts and a ⋯ menu', async () => {
    const { api, emit } = await open('accounts');
    const rows = document.querySelectorAll('.settings-account');
    expect(rows).toHaveLength(6);
    const primary = document.querySelector('.settings-account[data-key="claude:primary"]')!;
    expect(within(primary as HTMLElement).getByRole('note').textContent).toBe('Primary');
    expect(document.querySelector('.settings-account[data-key="claude:claude-lab"] .account-badge')?.textContent).toBe('Discovered');
    const work = document.querySelector('.settings-account[data-key="claude:work"]') as HTMLElement;
    expect(within(work).getByRole('switch', { name: 'Claude work' })).toBeTruthy();
    expect(work.querySelector('.usage-gauge')).toBeTruthy();
    fireEvent.click(within(work).getByRole('button', { name: 'More for Claude work' }));
    const [token, actions] = api.showPaneMenu.mock.calls.at(-1) as [string, string[]];
    expect(actions[0]).toBe('login');
    expect(actions[1]).toMatch(/^(enable|disable)$/);
    await emit('onPaneMenu', { token, action: actions[1] });
    expect(api.accountsToggle).toHaveBeenCalledWith('claude', 'work', actions[1] === 'enable');
    expect(screen.getByRole('button', { name: 'Add account…' })).toBeTruthy();
  });

  // The popover's usage fixture names the account claude-work; Settings lists it as work,
  // and its test clock is later, so the windows move to reset after it.
  const usageFor = (generation: number, percent: number) => {
    const next = envelope(generation);
    const work = (next.usage as typeof fixtures.usage).providers[0]!.accounts[0]!;
    const now = NOW / 1000;
    work.name = 'work';
    work.windows = work.windows.map((window) => ({ ...window, observed_at: now - 60, resets_at: now + 3600,
      used_percent: window.name === 'seven_day' ? percent : window.used_percent }));
    return next;
  };
  const work = () => document.querySelector('.settings-account[data-key="claude:work"]')!;
  const sliding = () => !!work().querySelector('.usage-gauge')?.classList.contains('is-fresh');
  const rolling = () => !!work().querySelector('.figure-strip, .figure-before');

  it('Settings keeps its reset clock and shows 2h ago below the row while retaining its Check button', async () => {
    const { api, emit } = await open('accounts');
    const next = usageFor(2, 86), data = next.usage as typeof fixtures.usage;
    data.providers[0]!.accounts[0]!.windows = data.providers[0]!.accounts[0]!.windows.map(window => ({
      ...window, stale: true, observed_at: NOW / 1000 - 7200,
    }));
    data.providers[0]!.accounts.find(account => account.name === 'claude-unknown')!.name = 'claude-lab';
    await emit('onSettingsStatus', next);
    const resetClock = new Date((NOW / 1000 + 3600) * 1000).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', hour12: false });
    expect(work().querySelector('.reset-time')?.textContent).toBe(resetClock);
    expect(work().querySelector('.account-usage-context')?.textContent).toBe('Last recorded usage, 2h ago · Terminal updates not set up');
    expect(work().getAttribute('data-tooltip')).toContain('2h ago');
    expect(work().querySelector('.window-tag, .row-link')).toBeNull();
    const unknown = document.querySelector('.settings-account[data-key="claude:claude-lab"]')!;
    const check = unknown.querySelector<HTMLButtonElement>('.row-action')!;
    expect(check.textContent).toBe('Check');
    fireEvent.click(check);
    await settle();
    expect(api.accountsCheck).toHaveBeenCalledWith('claude', 'claude-lab');
  });

  it('fresh usage slides the bars and rolls the digits as in the popover; status and older generations snap', async () => {
    const { emit } = await open('accounts');
    await emit('onSettingsStatus', usageFor(2, 86));
    expect(work().querySelector('.figure')?.textContent).toBe('86%');
    expect(sliding()).toBe(false);
    await emit('onUsageUpdated', usageFor(3, 88));
    expect(sliding()).toBe(true);
    expect(work().querySelector('.figure-strip')).not.toBeNull();
    await emit('onSettingsStatus', usageFor(4, 97));
    expect(sliding()).toBe(false);
    expect(rolling()).toBe(false);
    expect(work().querySelector('.figure')?.textContent).toBe('97%');
    await emit('onUsageUpdated', usageFor(3, 90));
    expect(rolling()).toBe(false);
  });

  it('reduced motion snaps the digits in Accounts', async () => {
    media.setReduced(true);
    const { emit } = await open('accounts');
    await emit('onSettingsStatus', usageFor(2, 86));
    await emit('onUsageUpdated', usageFor(3, 88));
    expect(rolling()).toBe(false);
    expect(work().querySelector('.figure')?.textContent).toBe('88%');
  });

  it('a rolling figure ending inside a flashing row leaves the flash running', async () => {
    const { emit } = await open('accounts');
    await emit('onSettingsTarget', { pane: 'accounts', runtime: 'claude', account: 'work' });
    await act(async () => { await new Promise((resolve) => requestAnimationFrame(resolve)); });
    expect(work().classList.contains('setting-flash')).toBe(true);
    fireEvent.animationEnd(work().querySelector('.usage-gauge')!);
    expect(work().classList.contains('setting-flash')).toBe(true);
    fireEvent.animationEnd(work());
    expect(work().classList.contains('setting-flash')).toBe(false);
  });

  it('keeps Add account… under a filtered, empty list', async () => {
    await open('accounts');
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'nobody-here' } });
    await settle();
    expect(screen.getByText('No results for “nobody-here” in Accounts.')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Add account…' })).toBeTruthy();
  });
});

describe('Sharing', () => {
  it('Sharing is off: banner with Copy enable command, which reads Copied for --copied-delay', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const { api } = await renderSettings();
      api.shareStatus.mockResolvedValue(structuredClone(statusOff));
      fireEvent.click(nav('sharing')!);
      await settle();
      expect(screen.getByText('Sharing is off.')).toBeTruthy();
      const button = screen.getByRole('button', { name: 'Copy enable command' });
      fireEvent.click(button);
      await settle();
      expect(api.copyText).toHaveBeenCalledWith('agt share enable --dry-run');
      expect(button.textContent).toBe('Copied');
      expect(announced()).toBe('Copied');
      await act(async () => { vi.advanceTimersByTime(cssTime('--copied-delay')); });
      expect(button.textContent).toBe('Copy enable command');
      expect(document.getElementById('setting-share-watcher')?.getAttribute('aria-disabled')).toBe('true');
    } finally { vi.useRealTimers(); }
  });

  it('Needs your choice is open; the grid has switches for editable cells and copy buttons otherwise', async () => {
    const { api } = await open('sharing');
    const waiting = screen.getByText('Needs your choice (2)').closest('details')!;
    expect(waiting.open).toBe(true);
    expect(document.querySelectorAll('.share-item')).toHaveLength(fixtures.shareList.items.length);
    const example = document.querySelector('.share-item[data-key="agents/example"]') as HTMLElement;
    const pick = within(example).getByRole('switch', { name: 'example in Codex' });
    expect(within(example).getByRole('button', { name: 'Copy command to remove example from Claude' })).toBeTruthy();
    fireEvent.click(pick);
    await settle();
    expect(api.sharePick).toHaveBeenCalledWith('agents/example', 'codex', false);
    expect(within(document.querySelector('.share-grid thead') as HTMLElement).getAllByRole('columnheader').map((cell) => cell.textContent)).toEqual(['Item', 'Claude', 'Codex']);
    expect([...document.querySelectorAll('.share-kind th')].map((cell) => cell.textContent)).toEqual(['Agents', 'MCP servers', 'Hooks', 'Instructions', 'Plugins']);
  });

  it('New items reads true as both kinds and writes on, off or the comma list', async () => {
    const { api } = await open('sharing');
    const button = document.getElementById('setting-share-auto_copy')!;
    expect(button.textContent).toBe('Every CLI');
    fireEvent.click(button);
    const menu = screen.getByRole('menu');
    expect(within(menu).getByRole('menuitemradio', { name: 'Every CLI' }).getAttribute('aria-checked')).toBe('true');
    expect(within(menu).getAllByRole('menuitemcheckbox').map((item) => item.getAttribute('aria-checked'))).toEqual(['true', 'true']);
    fireEvent.click(within(menu).getByRole('menuitemcheckbox', { name: 'Agents' }));
    await settle();
    expect(api.configSet).toHaveBeenLastCalledWith(expect.objectContaining({ key: 'share.auto_copy', value: 'skills', level: 'user', workspace: null }));
    fireEvent.click(button);
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'Wait for a choice' }));
    await settle();
    expect(api.configSet).toHaveBeenLastCalledWith(expect.objectContaining({ key: 'share.auto_copy', value: 'off' }));
  });

  it('Instructions shows readable labels and still writes agt’s raw choice', async () => {
    const { api } = await open('sharing');
    const button = document.getElementById('setting-share-instructions')!;
    expect(button.textContent).toBe('Not shared');
    fireEvent.click(button);
    expect(screen.getAllByRole('menuitemradio').map((item) => item.getAttribute('data-label'))).toEqual(['Not shared', 'agt’s block', 'Whole files']);
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'agt’s block' }));
    await settle();
    expect(api.configSet).toHaveBeenLastCalledWith(expect.objectContaining({ key: 'share.instructions', value: 'block', level: 'user', workspace: null }));
  });

  it('MCP servers switches the kind at once and shows the comma result', async () => {
    const { api } = await open('sharing');
    api.shareKind.mockResolvedValueOnce({ written: [1, 2], kept: [1], failed: [1], message: 'see the log' });
    fireEvent.click(within(settingRow('share.mcp')!).getByRole('switch'));
    await settle();
    expect(api.shareKind).toHaveBeenCalledWith('mcp', true);
    const result = document.querySelector('.inline-message.is-result')!;
    expect(result.textContent).toBe('2 shared, 1 stays, 1 failed, see the log');
    expect(result.querySelector('.is-failed')?.textContent).toBe('1 failed');
  });
});

describe('Activity', () => {
  it('groups by day; keyed rows go to their setting, plain rows are not focusable', async () => {
    const { api } = await open('activity');
    expect(document.querySelectorAll('.activity-row')).toHaveLength(fixtures.activity.entries.length);
    expect(document.querySelector('.activity-day')?.textContent).toBe('Today');
    const refused = document.querySelector('.activity-row .activity-marker');
    expect(refused?.textContent).toMatch(/^(Refused|\d+ failed)$/);
    const keyed = fixtures.activity.entries.find((entry) => entry.key === 'share.mcp')!;
    const row = document.querySelector(`.activity-row[data-key="${keyed.id}"]`) as HTMLElement;
    fireEvent.contextMenu(row);
    expect(api.showPaneMenu).toHaveBeenLastCalledWith(expect.any(String), ['revert', 'copy-key']);
    fireEvent.click(row.querySelector('button.activity-main')!);
    await settle();
    expect(document.body.dataset.pane).toBe('sharing');
    fireEvent.click(nav('activity')!);
    await settle();
    const plain = [...document.querySelectorAll('.activity-row')].find((node) => !node.querySelector('button'));
    expect(plain).toBeTruthy();
    expect(plain?.querySelector('[tabindex]')).toBeNull();
  });

  it('sharing runs show what changed where and their scope; labels are words; every row keeps the ⋯ column', async () => {
    await open('activity');
    const rowFor = (id: string) => document.querySelector<HTMLElement>(`.activity-row[data-key="${id}"]`)!;
    const run = fixtures.activity.entries.find((entry) => (entry as { run?: string }).run === 'fixture-run-2')!;
    const sync = rowFor(run.id);
    expect(sync.querySelector('.activity-label')?.textContent).toBe('Sharing');
    expect(sync.querySelector('.activity-change')?.textContent).toBe('notes, example@tools → codex');
    expect(sync.querySelector('.activity-level')?.textContent).toBe('User');
    expect(sync.dataset.tooltip).toContain('notes, example@tools → codex');
    const labels = [...document.querySelectorAll('.activity-label')].map((node) => node.textContent ?? '');
    expect(labels.filter((label) => /[._]/.test(label))).toEqual([]);
    expect(labels).toContain('Codex max jobs');
    expect(labels).toContain('Example kept out of');
    const instructions = fixtures.activity.entries.find((entry) => entry.key === 'share.instructions')!;
    expect(rowFor(instructions.id).querySelector('.activity-change')?.textContent).toBe('Not set → agt’s block');
    const long = fixtures.activity.entries.find((entry) => entry.key === 'claude.accounts' && Array.isArray(entry.old))!;
    const cell = rowFor(long.id).querySelector('.activity-change')!;
    expect(cell.querySelector('.middle-start')).toBeTruthy();
    expect(cell.querySelector('.middle-end')?.textContent?.length).toBeGreaterThan(0);
    expect(rowFor(long.id).dataset.tooltip).toContain(cell.textContent);
    for (const node of document.querySelectorAll('.activity-row')) {
      expect(node.querySelector(':scope > .activity-more, :scope > .activity-more-space')).toBeTruthy();
    }
  });
});

describe('S10 shared account-check results', () => {
  it('keeps Settings Check and its busy behaviour while sharing completed popover results', async () => {
    const { api, emit } = await open('accounts');
    const next = envelope(2), data = next.usage as typeof fixtures.usage;
    data.providers[0]!.accounts.find(account => account.name === 'claude-unknown')!.name = 'claude-lab';
    await emit('onSettingsStatus', next);
    const accountRow = () => document.querySelector('.settings-account[data-key="claude:claude-lab"]')!;
    const action = () => accountRow().querySelector<HTMLButtonElement>('.row-action')!;
    const check = { runtime: 'claude', account: 'claude-lab', phase: 'checking', result: null };
    await emit('onAccountChecksStatus', [check]);
    expect(action().textContent).toBe('Check');
    expect(accountRow().textContent).not.toContain('Checking…');
    await emit('onAccountChecksStatus', [{ ...check, phase: 'failed', result: { account: check.account, logged_in: null, checked_at: NOW / 1000, error: 'fake check failed' } }]);
    expect(action().textContent).toBe('Check');
    let finish = () => {};
    api.accountsCheck.mockImplementationOnce(() => new Promise<void>(resolve => { finish = resolve; }));
    fireEvent.click(action());
    await settle();
    expect(api.accountsCheck).toHaveBeenCalledWith('claude', 'claude-lab');
    expect(action().getAttribute('aria-busy')).toBe('true');
    await act(async () => finish());
    await settle();
    expect(action().getAttribute('aria-busy')).toBeNull();
    await emit('onAccountChecksStatus', [{ ...check, phase: 'complete', result: { account: check.account, logged_in: false, checked_at: NOW / 1000, error: null } }]);
    expect(action().textContent).toBe('Log in…');
    expect(api.accountsCheck).toHaveBeenCalledTimes(1);
  });
});
