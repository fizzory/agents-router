import { render, fireEvent, screen, within } from '@testing-library/react';
import { StrictMode } from 'react';
import { SettingsApp } from './SettingsApp';
import { describe, expect, it } from 'vitest';
import type { InstallStatus, UpdateStatus } from './api';
import { fakeSettingsApi, nav, NOW, renderSettings, settle } from '../test/settings';

const sheet = () => screen.getByRole('alertdialog');
const buttons = () => within(sheet()).queryAllByRole('button').map((button) => [button.textContent, button.classList.contains('button-default')]);
const key = async (name: string) => { fireEvent.keyDown(sheet(), { key: name }); await settle(); };

const INSTALLER: [InstallStatus, string, string, [string, boolean][], string | null, string | null][] = [
  [{ dialog: 'move' }, 'Move Agents Router to Applications?', 'It has to run from Applications to install agt.',
    [['Quit', false], ['Move to Applications', true]], 'move', 'quit'],
  [{ dialog: 'replace', version: '0.18.0', foreign: { target: '/x', version: '0.16.0', source: 'uv' } }, 'Replace agt 0.16.0 from uv?',
    'Agents Router installs agt 0.18.0. Restore previous agt in General switches back.', [['Keep', false], ['Replace', true]], 'replace', 'keep'],
  [{ dialog: 'downgrade', version: '0.18.0', bundle_version: '0.17.0' }, 'Install an older agt?', 'agt 0.18.0 is installed. This app has 0.17.0.',
    [['Cancel', true], ['Downgrade', false]], 'cancel', 'cancel'],
  [{ dialog: 'ready' }, 'agt is installed.', 'Finish setup to connect your CLIs.', [['Later', false], ['Set up in Terminal', true]], 'setup', 'later'],
  [{ dialog: 'uninstall' }, 'Uninstall agt?', 'Removes the agt this app installed. Your settings stay.', [['Cancel', true], ['Uninstall', false]], 'cancel', 'cancel'],
  [{ dialog: 'error', message: "Couldn't install agt. Free 12 MB." }, "Couldn't install agt.", 'Free 12 MB.', [['Close', false], ['Retry', true]], 'retry', 'close'],
  [{ dialog: 'error', message: "Install didn't finish. Your previous agt still works. Copy failed." }, "Install didn't finish. Your previous agt still works.",
    'Copy failed.', [['Close', false], ['Retry', true]], 'retry', 'close'],
  [{ dialog: 'progress', message: null }, 'Installing agt…', '', [], null, null],
];

describe('installer sheets', () => {
  it.each(INSTALLER)('%o', async (install, title, detail, expected, enter, escape) => {
    const { api } = await renderSettings({ install });
    expect(within(sheet()).getByRole('heading').textContent).toBe(title);
    if (detail) expect(document.getElementById('install-detail')?.textContent).toBe(detail);
    expect(buttons()).toEqual(expected);
    if (install.dialog === 'progress') expect(within(sheet()).getByRole('progressbar')).toBeTruthy();
    await key('Escape');
    await key('Enter');
    expect(api.installAction.mock.calls.map(([action]) => action)).toEqual([escape, enter].filter(Boolean));
    expect(document.querySelector('.sidebar')?.hasAttribute('inert')).toBe(true);
  });

  it('an IPC rejection shows as a red line in the sheet', async () => {
    const { api } = await renderSettings({ install: { dialog: 'ready' } });
    api.installAction.mockRejectedValueOnce(new Error('handoff failed'));
    fireEvent.click(screen.getByRole('button', { name: 'Set up in Terminal' }));
    await settle();
    expect(within(sheet()).getByRole('alert').textContent).toBe('handoff failed');
  });
});

const update = (dialog: UpdateStatus['dialog'], extra: Partial<UpdateStatus> = {}): UpdateStatus => ({ dialog, received: null, total: null, ...extra });

describe('updater sheets', () => {
  it('shows Rust’s copy verbatim; Return runs the default, Escape only offered close', async () => {
    const { api, emit } = await renderSettings({ update: update({ title: 'Update to 0.19.0?', detail: 'Agents Router restarts.',
      buttons: [['Later', 'close'], ['Update to 0.19.0', 'install']], default: 'install', busy: false }) });
    expect(within(sheet()).getByRole('heading').textContent).toBe('Update to 0.19.0?');
    expect(buttons()).toEqual([['Later', false], ['Update to 0.19.0', true]]);
    await key('Enter');
    expect(api.updateAction).toHaveBeenLastCalledWith('install');
    await key('Escape');
    expect(api.updateAction).toHaveBeenLastCalledWith('close');
    await emit('onUpdateStatus', update({ title: 'Couldn’t check for updates.', detail: 'Offline.', buttons: [['Open Releases Page', 'releases']],
      default: 'releases', busy: false }));
    api.updateAction.mockClear();
    await key('Escape');
    expect(api.updateAction).not.toHaveBeenCalled();
  });

  it('busy sheets show a progress bar: indeterminate, then MB of MB', async () => {
    const { emit } = await renderSettings({ update: update({ title: 'Checking for updates…', detail: '', buttons: [], default: 'close', busy: true }) });
    const bar = within(sheet()).getByRole('progressbar') as HTMLProgressElement;
    expect(bar.hasAttribute('value')).toBe(false);
    await emit('onUpdateStatus', update({ title: 'Downloading update…', detail: '', buttons: [], default: 'close', busy: true }, { received: 1048576, total: 4194304 }));
    const determinate = within(sheet()).getByRole('progressbar');
    expect(determinate.getAttribute('aria-valuetext')).toBe('1.0 of 4.0 MB');
    expect(within(sheet()).queryAllByRole('button')).toHaveLength(0);
  });
});

describe('sheet host', () => {
  it('shows one sheet at a time: installer, then updater, then Add account', async () => {
    const { emit } = await renderSettings({ install: { dialog: 'ready' }, target: { pane: 'accounts', account: '' },
      update: update({ title: 'You’re up to date.', detail: '', buttons: [['OK', 'close']], default: 'close', busy: false }) });
    expect(screen.getAllByRole('alertdialog')).toHaveLength(1);
    expect(within(sheet()).getByRole('heading').textContent).toBe('agt is installed.');
    await emit('onInstallStatus', { dialog: null });
    expect(within(sheet()).getByRole('heading').textContent).toBe('You’re up to date.');
    await emit('onUpdateStatus', update(null));
    expect(within(sheet()).getByRole('heading').textContent).toBe('Add account');
  });

  it('traps focus, and gives it back to the opener when the last sheet closes', async () => {
    await renderSettings();
    fireEvent.click(nav('accounts')!);
    await settle();
    const opener = screen.getByRole('button', { name: 'Add account…' });
    opener.focus();
    fireEvent.click(opener);
    await settle();
    expect(document.activeElement?.id).toBe('new-account-name');
    const cancel = within(sheet()).getByRole('button', { name: 'Cancel' });
    cancel.focus();
    fireEvent.keyDown(sheet(), { key: 'Tab', shiftKey: true });
    expect(sheet().contains(document.activeElement)).toBe(true);
    await key('Escape');
    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(document.activeElement).toBe(opener);
  });
});

describe('Add account', () => {
  it('checks the name, submits with Return, and after a failure checks again by itself', async () => {
    const { api } = await renderSettings({ target: { pane: 'accounts', account: '' } });
    const add = () => within(sheet()).getByRole('button', { name: 'Add account…' });
    expect(add().hasAttribute('disabled')).toBe(true);
    let resolve: (value: unknown) => void = () => {};
    api.accountsAddCheck.mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'lab' } });
    expect(document.getElementById('account-name-message')?.textContent).toBe('Checking name…');
    resolve({ ok: true, account: 'lab', runtime: 'claude' });
    await settle();
    expect(add().hasAttribute('disabled')).toBe(false);
    api.accountsAdd.mockRejectedValueOnce(new Error('Terminal did not open'));
    await key('Enter');
    expect(api.accountsAdd).toHaveBeenCalledWith('claude', 'lab');
    expect(document.getElementById('account-name-message')?.textContent).toBe('Terminal did not open');
    expect(document.activeElement?.id).toBe('new-account-name');
    expect(api.accountsAddCheck).toHaveBeenCalledTimes(2);
    fireEvent.click(add());
    await settle();
    expect(api.accountsAdd).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole('alertdialog')).toBeNull();
  });

  it('Return on a Provider menu item picks it and doesn’t press Add account…', async () => {
    const { api } = await renderSettings({ target: { pane: 'accounts', account: '' } });
    api.accountsAddCheck.mockResolvedValue({ ok: true, account: 'lab', runtime: 'claude' });
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'lab' } });
    await settle();
    fireEvent.click(document.getElementById('add-account-provider')!);
    const codex = screen.getByRole('menuitemradio', { name: 'Codex' });
    codex.focus();
    // The sheet must leave Return to the menu item, whose native activation is a click.
    expect(fireEvent.keyDown(codex, { key: 'Enter' })).toBe(true);
    await settle();
    expect(api.accountsAdd).not.toHaveBeenCalled();
    fireEvent.click(codex);
    await settle();
    expect(document.getElementById('add-account-provider')?.textContent).toContain('Codex');
    expect(screen.getByRole('alertdialog')).toBeTruthy();
    fireEvent.click(document.getElementById('add-account-provider')!);
    expect(fireEvent.keyDown(screen.getByRole('menuitemradio', { name: 'Claude' }), { key: 'Escape' })).toBe(false);
    await settle();
    expect(screen.getByRole('alertdialog')).toBeTruthy();
  });

  it('name checks still land under StrictMode', async () => {
    const fake = fakeSettingsApi({ target: { pane: 'accounts', account: '' } });
    render(<StrictMode><SettingsApp api={fake.api} now={() => NOW} timeZone="UTC" /></StrictMode>);
    await settle();
    fake.api.accountsAddCheck.mockResolvedValue({ ok: true, account: 'lab', runtime: 'claude' });
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'lab' } });
    await settle();
    expect(document.getElementById('account-name-message')?.textContent).toBe('');
    expect(within(sheet()).getByRole('button', { name: 'Add account…' }).hasAttribute('disabled')).toBe(false);
  });

  it('offers Claude, Codex and Antigravity, not Kiro', async () => {
    await renderSettings({ target: { pane: 'accounts', account: '' } });
    fireEvent.click(document.getElementById('add-account-provider')!);
    expect(screen.getAllByRole('menuitemradio').map((item) => item.getAttribute('data-label'))).toEqual(['Claude', 'Codex', 'Antigravity']);
  });
});
