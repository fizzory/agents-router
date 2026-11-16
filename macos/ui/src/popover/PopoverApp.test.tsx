import { act, fireEvent, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { cssTime } from '../shared/motion';
import { media, resizeObservers } from '../test/dom';
import { envelope, hide, renderPopover, row, show, usage } from '../test/popover';

const accounts = usage.providers.flatMap((provider) => provider.accounts.map((account) => `${provider.runtime}:${account.name}`));
const button = (name: string | RegExp) => screen.getByRole('button', { name });
const slot = (key: string) => row(key)?.querySelector('.row-slot')?.textContent;
const statusText = () => document.getElementById('status')?.textContent;
const visible = (id: string) => !document.getElementById(id)?.hidden;

describe('§9.1 popover envelope cases', () => {
  it('first: Loading accounts…, Refresh busy', async () => {
    await renderPopover(envelope('first'));
    expect(visible('reading')).toBe(true);
    expect(document.getElementById('reading')?.textContent).toBe('Loading accounts…');
    expect(document.getElementById('refresh')?.getAttribute('aria-label')).toBe('Refreshing usage');
    expect(document.getElementById('refresh')?.getAttribute('aria-busy')).toBe('true');
    expect(statusText()).toBe('Loading accounts…');
  });

  it('reading: rows stay while Refresh spins and says Refreshing usage', async () => {
    const { api } = await renderPopover(envelope('reading'));
    expect(document.querySelectorAll('#accounts .account-row')).toHaveLength(accounts.length);
    expect(button('Refreshing usage').getAttribute('aria-busy')).toBe('true');
    fireEvent.click(button('Refreshing usage'));
    expect(api.refreshUsage).not.toHaveBeenCalled();
  });

  it('empty: No accounts yet. and Add account… opens Settings for a new account', async () => {
    const { api } = await renderPopover(envelope('empty'));
    expect(screen.getByText('No accounts yet.')).toBeTruthy();
    fireEvent.click(button('Add account…'));
    expect(api.openSettings).toHaveBeenCalledWith({ account: '' });
  });

  it('unreadable: Can’t read agt. with the detail line and Retry', async () => {
    const { api } = await renderPopover(envelope('unreadable'));
    const footer = document.getElementById('last-row');
    expect(footer?.querySelector('.footer-message')?.textContent).toBe("Can't read agt.");
    expect(footer?.querySelector('.footer-detail')?.textContent).toBe("Can't read agt: timed out");
    const retry = button("Can't read agt. Retry");
    expect(retry.getAttribute('aria-describedby')).toBe('footer-detail');
    fireEvent.click(retry);
    expect(api.refreshUsage).toHaveBeenCalledTimes(1);
    expect(statusText()).toBe("Can't read agt. Can't read agt: timed out");
  });

  it('missing and old: install or repair through Settings; the pending update stays hidden', async () => {
    for (const [name, text, action] of [['missing', "agt isn't installed.", 'Install'], ['old', 'agt 0.16 is too old.', 'Repair']] as const) {
      const { api, unmount } = await renderPopover(envelope(name));
      expect(document.querySelector('.footer-message')?.textContent).toBe(text);
      expect(document.querySelector('.footer-detail')).toBeNull();
      expect(screen.queryByText(/Update to/)).toBeNull();
      fireEvent.click(button(`${text} ${action}`));
      expect(api.openSettings).toHaveBeenCalledWith({ install: true });
      unmount();
    }
  });

  it('update: the footer row offers Update to 0.17.1…', async () => {
    const { api } = await renderPopover(envelope('update'));
    fireEvent.click(button('Update to 0.17.1…'));
    expect(api.checkForUpdates).toHaveBeenCalledTimes(1);
    expect(statusText()).toBe('Update to 0.17.1…');
  });

  it('nothing: the header names the earliest return', async () => {
    await renderPopover(envelope('nothing'));
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('No account ready until 20:15');
    expect(statusText()).toContain('No account ready until 20:15');
  });

  it('an IPC failure shows in the footer with Retry and its message', async () => {
    const { api } = await renderPopover();
    api.openSettings.mockImplementationOnce(() => Promise.reject(new Error('IPC down')));
    await act(async () => { fireEvent.click(button('Settings…')); });
    expect(document.querySelector('.footer-detail')?.textContent).toBe('Error: IPC down');
  });
});

describe('§9.2 availability states', () => {
  it('each state has its dot and slot copy', async () => {
    await renderPopover();
    const expected: Record<string, [string, string]> = {
      'claude:claude-work': ['ok', '86%Wed'], 'claude:claude-cooling': ['cooling', 'Back 21:40'],
      'claude:claude-logged-out': ['logged_out', 'Log in…'], 'claude:claude-unknown': ['unknown', 'Check'],
      'claude:claude-busy': ['busy', '2 of 4 jobs'], 'claude:claude-off': ['off', 'Off'],
      'claude:claude-unhealthy': ['unhealthy', 'Back 19:02'], 'claude:claude-recovering': ['recovering', 'Recovering'],
      'claude:claude-malformed': ['unknown', 'Unknown'], 'claude:claude-stale': ['ok', '86%2 h ago'],
      'kiro:primary': ['ok', ''], 'antigravity:primary': ['ok', ''],
    };
    for (const [key, [state, text]] of Object.entries(expected)) {
      expect(row(key)?.querySelector('.status-dot')?.getAttribute('data-state'), key).toBe(state);
      expect(slot(key), key).toBe(text);
    }
    expect(row('claude:claude-malformed')?.querySelector('button.row-action')).toBeNull();
    expect(row('kiro:primary')?.querySelector('.usage-gauge')?.classList.contains('is-hidden')).toBe(true);
  });

  it('keeps the selftest and smoke contracts: rows, labels, headings outside the count', async () => {
    await renderPopover();
    const rows = document.querySelectorAll('#accounts .account-row');
    expect(rows).toHaveLength(accounts.length);
    const labels = [...document.querySelectorAll('#accounts .row-link')].map((node) => node.getAttribute('aria-label') ?? '');
    for (const name of usage.providers.flatMap((provider) => provider.accounts.map((account) => account.name))) {
      expect(labels.some((label) => label.includes(name))).toBe(true);
    }
    expect(labels.every((label) => label.endsWith('Open in Settings.'))).toBe(true);
    const headings = [...document.querySelectorAll('#accounts .group-heading')].map((node) => node.textContent);
    expect(headings).toEqual(['Claude', 'Codex', 'Antigravity', 'Kiro']);
    for (const part of row('claude:claude-work')?.querySelectorAll('.status-dot, .account-name, .usage-gauge, .rolling-figure') ?? []) {
      expect(part.getAttribute('aria-hidden')).toBe('true');
    }
  });
});

describe('keyboard', () => {
  it('Tab: Refresh, Settings, More, the list as one stop, then the footer', async () => {
    const user = userEvent.setup();
    await renderPopover(envelope('update'));
    for (const name of ['Refresh usage', 'Settings…', 'More']) {
      await user.tab();
      expect(document.activeElement).toBe(button(name));
    }
    await user.tab();
    expect(document.activeElement?.closest('.account-row')?.getAttribute('data-key')).toBe('claude:claude-work');
    await user.tab();
    expect(document.activeElement).toBe(button('Update to 0.17.1…'));
  });

  it('↑/↓, Home/End move between rows without wrapping; ←/→ move inside a row', async () => {
    const { api } = await renderPopover();
    const first = row('claude:claude-work')?.querySelector<HTMLButtonElement>('.row-link');
    act(() => first?.focus());
    const key = () => document.activeElement?.closest('.account-row')?.getAttribute('data-key');
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowUp' });
    expect(key()).toBe('claude:claude-work');
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' });
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' });
    expect(key()).toBe('claude:claude-logged-out');
    // A row with an inline button is entered on the button.
    expect(document.activeElement?.classList.contains('row-action')).toBe(true);
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowLeft' });
    expect(document.activeElement?.classList.contains('row-link')).toBe(true);
    fireEvent.keyDown(document.activeElement!, { key: 'Enter' });
    fireEvent.click(document.activeElement!);
    expect(api.openSettings).toHaveBeenCalledWith({ runtime: 'claude', account: 'claude-logged-out' });
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowRight' });
    expect(document.activeElement?.classList.contains('row-action')).toBe(true);
    fireEvent.keyDown(document.activeElement!, { key: 'End' });
    expect(key()).toBe('kiro:primary');
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' });
    expect(key()).toBe('kiro:primary');
    fireEvent.keyDown(document.activeElement!, { key: 'Home' });
    expect(key()).toBe('claude:claude-work');
    // Exactly one tab stop in the whole list.
    expect(document.querySelectorAll('#accounts [tabindex="0"]')).toHaveLength(1);
  });

  it('a status update keeps the focused row node and its focus', async () => {
    const { status } = await renderPopover();
    const link = row('claude:claude-busy')?.querySelector<HTMLButtonElement>('.row-link');
    act(() => link?.focus());
    await status({ ...envelope('reading'), generation: 2 });
    expect(row('claude:claude-busy')?.querySelector('.row-link')).toBe(link);
    expect(document.activeElement).toBe(link);
  });

  it('⌘R, ⌘, and ⌘Q on document run Refresh, Settings and Quit', async () => {
    const { api, status } = await renderPopover();
    fireEvent.keyDown(document, { key: 'r', metaKey: true });
    fireEvent.keyDown(document, { key: ',', metaKey: true });
    fireEvent.keyDown(document, { key: 'q', metaKey: true });
    expect(api.refreshUsage).toHaveBeenCalledTimes(1);
    expect(api.openSettings).toHaveBeenCalledWith();
    expect(api.quitApp).toHaveBeenCalledTimes(1);
    await status({ ...envelope('reading'), generation: 2 });
    fireEvent.keyDown(document, { key: 'r', metaKey: true });
    expect(api.refreshUsage).toHaveBeenCalledTimes(1);
  });

  it('Escape: closes the More menu first, then hides the tooltip and the popover', async () => {
    const { api } = await renderPopover();
    fireEvent.click(button('More'));
    const menu = screen.getByRole('menu');
    expect(menu.hidden).toBe(false);
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
    expect(menu.hidden).toBe(true);
    expect(document.activeElement).toBe(button('More'));
    expect(api.hidePopover).not.toHaveBeenCalled();
    act(() => row('claude:claude-work')?.querySelector<HTMLButtonElement>('.row-link')?.focus());
    expect(screen.getByRole('tooltip').hidden).toBe(false);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.getByRole('tooltip', { hidden: true }).hidden).toBe(true);
    expect(api.hidePopover).toHaveBeenCalledTimes(1);
  });
});

describe('More menu', () => {
  it('roving focus, type-ahead, Tab and outside clicks close it; focus returns to More on Escape', async () => {
    const { api } = await renderPopover();
    const more = button('More');
    fireEvent.click(more);
    expect(more.getAttribute('aria-expanded')).toBe('true');
    const menu = screen.getByRole('menu');
    const [check, quit] = within(menu).getAllByRole('menuitem');
    expect(document.activeElement).toBe(check);
    fireEvent.keyDown(check!, { key: 'ArrowDown' });
    expect(document.activeElement).toBe(quit);
    fireEvent.keyDown(quit!, { key: 'ArrowDown' });
    expect(document.activeElement).toBe(check);
    fireEvent.keyDown(check!, { key: 'End' });
    expect(document.activeElement).toBe(quit);
    fireEvent.keyDown(quit!, { key: 'Home' });
    expect(document.activeElement).toBe(check);
    fireEvent.keyDown(check!, { key: 'q' });
    expect(document.activeElement).toBe(quit);
    expect(quit?.getAttribute('aria-keyshortcuts')).toBe('Meta+Q');
    expect(quit?.textContent).toContain('Quit Agents Router');
    fireEvent.keyDown(quit!, { key: 'Tab' });
    expect(menu.hidden).toBe(true);
    fireEvent.click(more);
    fireEvent.pointerDown(document.body);
    expect(menu.hidden).toBe(true);
    fireEvent.click(more);
    fireEvent.click(within(menu).getByRole('menuitem', { name: 'Check for Updates…' }));
    expect(api.checkForUpdates).toHaveBeenCalledTimes(1);
    expect(menu.hidden).toBe(true);
    fireEvent.click(more);
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
    expect(document.activeElement).toBe(more);
  });
});

describe('inline actions', () => {
  it('Log in shows its spinner only after --terminal-delay and ignores repeat clicks', async () => {
    const { api } = await renderPopover();
    let finish = () => {};
    api.loginAccount.mockImplementation(() => new Promise((resolve) => { finish = () => resolve(undefined); }));
    const login = row('claude:claude-logged-out')!.querySelector<HTMLButtonElement>('.row-action')!;
    fireEvent.click(login);
    fireEvent.click(login);
    expect(api.loginAccount).toHaveBeenCalledTimes(1);
    expect(login.getAttribute('aria-busy')).toBe('true');
    expect(login.getAttribute('aria-label')).toBe('Opening Terminal for claude-logged-out');
    expect(login.querySelector('.turning-icon')).toBeNull();
    await act(() => new Promise((resolve) => setTimeout(resolve, cssTime('--terminal-delay') + 20)));
    expect(login.querySelector('.turning-icon')).not.toBeNull();
    await act(async () => finish());
    expect(login.getAttribute('aria-busy')).toBeNull();
    expect(login.querySelector('.turning-icon')).toBeNull();
  });

  it('Check shows its spinner at once and says Checking', async () => {
    const { api } = await renderPopover();
    api.checkAccount.mockImplementation(() => new Promise(() => {}));
    const check = row('claude:claude-unknown')!.querySelector<HTMLButtonElement>('.row-action')!;
    fireEvent.click(check);
    expect(api.checkAccount).toHaveBeenCalledWith('claude', 'claude-unknown');
    expect(check.getAttribute('aria-label')).toBe('Checking claude-unknown');
    expect(check.querySelector('.turning-icon')).not.toBeNull();
  });
});

describe('fresh usage, motion and lifetime', () => {
  const bumped = (generation: number, percent: number) => {
    const next = envelope(null, generation);
    const data = next.usage as typeof usage;
    const work = data.providers[0]!.accounts[0]!;
    work.windows = work.windows.map((window) => (window.name === 'seven_day' ? { ...window, used_percent: percent } : window));
    return next;
  };

  it('only fresh usage on a showing panel slides bars and rolls digits; status snaps; older generations are ignored', async () => {
    const { fresh, status } = await renderPopover();
    show();
    await fresh(bumped(2, 88));
    const work = row('claude:claude-work')!;
    expect(work.querySelector('.usage-gauge')?.classList.contains('is-fresh')).toBe(true);
    expect(work.querySelector('.figure-strip')).not.toBeNull();
    await status(bumped(3, 97));
    expect(work.querySelector('.usage-gauge')?.classList.contains('is-fresh')).toBe(false);
    expect(work.querySelector('.figure-strip')).toBeNull();
    expect(work.querySelector('.figure')?.textContent).toBe('97%');
    // 95% or more: semibold warn figure and an amber main bar.
    expect(work.querySelector('.figure')?.classList.contains('is-warn')).toBe(true);
    expect(work.querySelector('.gauge-bar.is-main')?.classList.contains('is-warn')).toBe(true);
    await fresh(bumped(2, 10));
    expect(work.querySelector('.figure')?.textContent).toBe('97%');
  });

  it('reduced motion snaps the digits', async () => {
    media.setReduced(true);
    const { fresh } = await renderPopover();
    show();
    await fresh(bumped(2, 88));
    const work = row('claude:claude-work')!;
    expect(work.querySelector('.figure-strip, .figure-before')).toBeNull();
    expect(work.querySelector('.figure')?.textContent).toBe('88%');
  });

  it('a hidden panel never animates and settles a running roll', async () => {
    const { fresh } = await renderPopover();
    show();
    await fresh(bumped(2, 88));
    hide();
    const work = row('claude:claude-work')!;
    expect(document.body.dataset.active).toBe('false');
    expect(work.querySelector('.figure-strip')).toBeNull();
    await fresh(bumped(3, 90));
    expect(work.querySelector('.figure-strip')).toBeNull();
  });

  it('reports the intrinsic height and removes every listener on unmount', async () => {
    // jsdom has no layout: every visible part is 20px tall.
    vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockReturnValue(20);
    const { api, unlistened, unmount, status } = await renderPopover();
    await act(async () => { await Promise.resolve(); });
    // Loading, then the first answer (header and list): both snap.
    const sizes = () => api.setPopoverHeight.mock.calls.map(([size]) => size);
    expect(sizes().every((size) => size.durationMs === 0)).toBe(true);
    expect(sizes().at(-1)?.height).toBe(40);
    // A later change (the update footer appears) animates with --panel-resize-time.
    await status({ ...envelope('update'), generation: 2 });
    await act(async () => { await Promise.resolve(); });
    expect(sizes().at(-1)).toMatchObject({ height: 60, durationMs: cssTime('--panel-resize-time') });
    const observer = resizeObservers.at(-1)!;
    expect(observer.observed.size).toBe(5);
    unmount();
    expect(unlistened.sort()).toEqual(['popover-status', 'usage-updated']);
    expect(observer.disconnected).toBe(true);
    fireEvent.keyDown(document, { key: 'Escape' });
    fireEvent.keyDown(document, { key: 'q', metaKey: true });
    expect(api.hidePopover).not.toHaveBeenCalled();
    expect(api.quitApp).not.toHaveBeenCalled();
  });
});
