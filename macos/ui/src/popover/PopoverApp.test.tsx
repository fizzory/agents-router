import { act, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { cssTime } from '../shared/motion';
import { SKELETON_ROWS_KEY } from '../../logic/skeleton.js';
import { media, resizeObservers } from '../test/dom';
import { envelope, fakeApi, hide, NOW, renderPopover, row, show, updateStatus, usage } from '../test/popover';
import failures from '../../../../tests/fixtures/popover/update-failures.json';
import popoverStyles from './popover.css?raw';
import sharedStyles from '../shared/shared.css?raw';

const accounts = usage.providers.flatMap((provider) => provider.accounts.map((account) => `${provider.runtime}:${account.name}`));
const button = (name: string | RegExp) => screen.getByRole('button', { name });
const slot = (key: string) => row(key)?.querySelector('.row-slot')?.textContent;
const statusText = () => document.getElementById('status')?.textContent;
const visible = (id: string) => !document.getElementById(id)?.hidden;

describe('§9.1 popover envelope cases', () => {
  it('first: three static skeleton rows, Loading accounts… announced, Refresh busy', async () => {
    await renderPopover(envelope('first'));
    show();
    expect(visible('reading')).toBe(true);
    const skeleton = document.getElementById('reading')!;
    expect(skeleton.getAttribute('aria-hidden')).toBe('true');
    expect(skeleton.querySelectorAll('.skeleton-row')).toHaveLength(3);
    expect(skeleton.textContent).toBe('');
    expect(skeleton.querySelectorAll('.skeleton-dot, .skeleton-name, .skeleton-gauge, .skeleton-slot')).toHaveLength(12);
    expect(document.getElementById('refresh')?.getAttribute('aria-label')).toBe('Refreshing usage');
    expect(document.getElementById('refresh')?.getAttribute('aria-busy')).toBe('true');
    expect(document.querySelectorAll('.is-turning')).toHaveLength(1);
    expect(document.querySelector('#refresh .is-turning')).not.toBeNull();
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

  it('unreadable: the problem is the summary, with a full alert directly under the header and Retry', async () => {
    const { api } = await renderPopover(envelope('unreadable'));
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe("Can't read agt");
    expect(screen.getByRole('alert').textContent).toBe("Can't read agt: timed out");
    expect(document.querySelector('.popover-head')?.nextElementSibling?.id).toBe('trouble-row');
    const retry = button('Retry');
    expect(retry.getAttribute('aria-describedby')).toBe('trouble-detail');
    fireEvent.click(retry);
    expect(api.refreshUsage).toHaveBeenCalledTimes(1);
    expect(statusText()).toBe("Can't read agt. Can't read agt: timed out");
    expect(visible('last-row')).toBe(false);
  });

  it('missing and old: install or repair through Settings; the pending update stays hidden', async () => {
    for (const [name, text, action] of [['missing', "agt isn't installed", 'Install…'], ['old', 'agt 0.16 is too old', 'Repair…']] as const) {
      const { api, unmount } = await renderPopover(envelope(name));
      expect(screen.getByRole('heading', { level: 1 }).textContent).toBe(text);
      expect(screen.getByRole('alert').textContent).toBeTruthy();
      expect(screen.queryByText(/is available/)).toBeNull();
      expect(visible('last-row')).toBe(false);
      fireEvent.click(button(action));
      expect(api.openSettings).toHaveBeenCalledWith({ install: true });
      unmount();
    }
  });

  it('update: the footer row says 0.17.1 is available with an accent dot and Install and Restart', async () => {
    const { api } = await renderPopover(envelope('update'));
    const footer = document.getElementById('last-row')!;
    expect(visible('last-row')).toBe(true);
    expect(footer.querySelector('.update-dot')).not.toBeNull();
    expect(footer.querySelector('.update-text')?.textContent).toBe('0.17.1 is available');
    expect(button('Install and Restart').closest('#last-row')).toBe(footer);
    expect(statusText()).toBe('6 ready. 0.17.1 is available');
    expect(api.getUpdateStatus).toHaveBeenCalledTimes(1);
  });

  it('nothing: an automatic check takes priority over the earliest return', async () => {
    await renderPopover(envelope('nothing'));
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Checking accounts…');
    expect(statusText()).toContain('Checking accounts…');
  });

  it('an IPC failure shows in the trouble row with Retry and its full message', async () => {
    const { api } = await renderPopover();
    api.openSettings.mockImplementationOnce(() => Promise.reject(new Error('IPC down')));
    await act(async () => { fireEvent.click(button('Settings…')); });
    expect(screen.getByRole('alert').textContent).toContain('Error: IPC down');
  });
});

describe('first-read skeleton row count', () => {
  const skeleton = () => document.querySelectorAll('#reading .skeleton-row');
  const withRows = (count: number, generation = 2) => ({ ...envelope(null, generation), usage: {
    ...usage, providers: [{ ...usage.providers[0]!, accounts: Array.from({ length: count }, (_, index) => ({
      ...usage.providers[0]!.accounts[0]!, name: `account-${index}`,
    })) }],
  } });

  it.each([['5', 5], ['0', 1], ['9', 6], ['garbage', 3], ['', 3]])('stored %j gives %i skeleton rows', async (stored, count) => {
    window.localStorage.setItem(SKELETON_ROWS_KEY, stored);
    await renderPopover(envelope('first'));
    expect(skeleton()).toHaveLength(count);
    expect(window.localStorage.getItem(SKELETON_ROWS_KEY)).toBe(stored);
  });

  it('defaults to three rows when localStorage access throws', async () => {
    vi.spyOn(window, 'localStorage', 'get').mockImplementation(() => { throw new Error('storage denied'); });
    await renderPopover(envelope('first'));
    expect(skeleton()).toHaveLength(3);
    expect(statusText()).toBe('Loading accounts…');
  });

  it.each([[0, '1'], [2, '2'], [9, '6']])('a successful snapshot with %i account rows stores %s', async (count, stored) => {
    window.localStorage.setItem(SKELETON_ROWS_KEY, '5');
    const { status, unmount } = await renderPopover(envelope('first'));
    expect(window.localStorage.getItem(SKELETON_ROWS_KEY)).toBe('5');
    await status(withRows(count));
    expect(window.localStorage.getItem(SKELETON_ROWS_KEY)).toBe(stored);
    unmount();
    await renderPopover(envelope('first'));
    expect(skeleton()).toHaveLength(Number(stored));
  });

  it('keeps rendering a successful snapshot when storing its count throws', async () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('storage full'); });
    const { status } = await renderPopover(envelope('first'));
    await status(withRows(2));
    expect(document.querySelectorAll('#accounts .account-row')).toHaveLength(2);
    expect(visible('reading')).toBe(false);
    expect(visible('trouble-row')).toBe(false);
  });

  it.each([
    ['first loading', envelope('first')],
    ['reading with usage', { ...withRows(2), reading: true }],
    ['error without usage', envelope('unreadable')],
    ['error with usage', { ...withRows(2), error: 'usage failed' }],
    ['missing agt', envelope('missing')],
    ['old agt', envelope('old')],
    ['invalid usage', { ...envelope(null), usage: { invalid: true } }],
  ])('%s does not overwrite the remembered count', async (_name, first) => {
    window.localStorage.setItem(SKELETON_ROWS_KEY, '5');
    const write = vi.spyOn(Storage.prototype, 'setItem');
    await renderPopover(first);
    expect(window.localStorage.getItem(SKELETON_ROWS_KEY)).toBe('5');
    expect(write).not.toHaveBeenCalled();
  });
});

describe('§9.2 availability states', () => {
  it('idle ok and recovering rows have solid marks without breathing', async () => {
    await renderPopover();
    for (const key of ['claude:claude-work', 'claude:claude-recovering']) {
      const mark = row(key)?.querySelector('.status-dot');
      expect(mark?.getAttribute('data-shape'), key).toBe('solid');
      expect(mark?.getAttribute('data-breathing'), key).toBe('false');
    }
  });

  it('each fixture row renders its expected mark shape', async () => {
    await renderPopover();
    const expected: Record<string, string> = {
      'claude:claude-work': 'solid', 'claude:claude-cooling': 'half',
      'claude:claude-logged-out': 'ring', 'claude:claude-unknown': 'hollow',
      'claude:claude-busy': 'half', 'claude:claude-off': 'hollow',
      'claude:claude-stale': 'solid', 'claude:claude-reset': 'solid',
      'claude:claude-unhealthy': 'ring', 'claude:claude-recovering': 'solid',
      'claude:claude-malformed': 'hollow', 'codex:primary': 'hollow',
      'antigravity:primary': 'solid', 'kiro:primary': 'solid',
    };
    expect(Object.keys(expected).sort()).toEqual([...accounts].sort());
    for (const [key, shape] of Object.entries(expected)) {
      expect(row(key)?.querySelector('.status-dot')?.getAttribute('data-shape'), key).toBe(shape);
    }
  });

  it('summary counts solid marks and every running-job state breathes only with room', async () => {
    const { status } = await renderPopover();
    for (const availability of ['ok', 'recovering', 'busy', 'cooling', 'logged_out', 'unhealthy', 'unknown', 'off']) {
      for (const full of [false, true]) {
        const next = envelope(null, 2);
        const data = next.usage as typeof usage;
        const account = { ...data.providers[0]!.accounts[0]!, availability, running_jobs: 2, busy_slots: full ? 4 : 2, capacity: 4,
          enabled: availability !== 'off' };
        next.usage = { ...data, providers: [{ ...data.providers[0]!, accounts: [account] }] };
        await status(next);
        const available = ['ok', 'recovering', 'busy'].includes(availability) && !full;
        const solid = document.querySelectorAll('#accounts [data-shape="solid"]').length;
        expect(solid, `${availability}/${full}`).toBe(Number(available));
        expect(row(`claude:${account.name}`)?.querySelector('.status-dot')?.getAttribute('data-breathing')).toBe(String(available));
        if (available) expect(screen.getByRole('heading', { level: 1 }).textContent).toBe(`${solid} ready`);
        if (['ok', 'recovering', 'busy'].includes(availability) && full) {
          expect(document.querySelector('.status-dot')?.getAttribute('data-shape')).toBe('half');
        }
      }
    }
  });

  it('each state has its dot and slot copy', async () => {
    await renderPopover();
    const expected: Record<string, [string, string]> = {
      'claude:claude-work': ['ok', 'wk86%1m ago'], 'claude:claude-cooling': ['cooling', 'Back 21:40'],
      'claude:claude-logged-out': ['logged_out', 'Log in…'], 'claude:claude-unknown': ['unknown', 'Checking…'],
      'claude:claude-busy': ['busy', '2 of 4 jobs'], 'claude:claude-off': ['off', 'Off'],
      'claude:claude-unhealthy': ['unhealthy', 'Back 19:02'], 'claude:claude-recovering': ['recovering', 'Recovering'],
      'claude:claude-malformed': ['unknown', 'Unknown'], 'claude:claude-stale': ['ok', 'wk86%2h ago'],
      'kiro:primary': ['ok', 'Ready'], 'antigravity:primary': ['ok', 'Ready'],
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
    expect(labels.every((label) => label.endsWith('Show account detail.'))).toBe(true);
    const headings = [...document.querySelectorAll('#accounts .group-heading')].map((node) => node.textContent);
    expect(headings).toEqual(['Claude', 'Codex', 'Antigravity', 'Kiro']);
    for (const part of row('claude:claude-work')?.querySelectorAll('.status-dot, .account-name, .usage-gauge, .rolling-figure') ?? []) {
      expect(part.getAttribute('aria-hidden')).toBe('true');
    }
  });
});

describe('in-place detail', () => {
  it('uses two right-aligned 14px Claude lines in the existing 96 × 28 slot, and keeps state slots single-line', async () => {
    render(<style>{sharedStyles}{popoverStyles}</style>);
    await renderPopover();
    const target = row('claude:claude-work')!;
    const slot = target.querySelector<HTMLElement>('.row-slot')!;
    expect(slot.classList.contains('is-claude-usage')).toBe(true);
    expect(slot.querySelector('.usage-figure-line')?.textContent).toBe('wk86%');
    expect(slot.querySelector('.observation-age')?.textContent).toBe('1m ago');
    expect(slot.querySelector('.observation-age')?.getAttribute('aria-hidden')).toBe('true');
    expect(getComputedStyle(slot).width).toBe('96px');
    expect(getComputedStyle(slot).height).toBe('28px');
    expect(getComputedStyle(slot).flexDirection).toBe('column');
    expect(getComputedStyle(slot).alignItems).toBe('flex-end');
    for (const part of ['.usage-figure-line', '.observation-age']) expect(getComputedStyle(slot.querySelector(part)!).height).toBe('14px');
    expect(getComputedStyle(slot.querySelector('.observation-age')!).textAlign).toBe('right');
    expect(target.querySelector('.row-link')?.getAttribute('aria-label')).toBe('Claude claude-work. Available. 5h 17%, as of your last terminal Claude Code reply, 1m ago, resets 20:00. Week 86%, last recorded usage, 1m ago, resets Wed 19:00. Show account detail.');
    for (const key of ['claude:claude-cooling', 'claude:claude-logged-out', 'claude:claude-unknown', 'kiro:primary']) {
      expect(row(key)?.querySelector('.is-claude-usage, .usage-figure-line, .observation-age')).toBeNull();
    }
    for (const target of document.querySelectorAll('.account-row[data-key^="claude:"]:has(.figure)')) {
      expect(target.querySelector('.observation-age')?.textContent).toMatch(/(?:\d+[mhd] ago|age unknown)$/);
    }
  });

  it('shows terminal age, stale/recorded labels and unknown age through component updates', async () => {
    const { status } = await renderPopover();
    let generation = 2;
    for (const [source, observed_at, age, stale, context] of [
      ['terminal', NOW - 60, '1m ago', false, 'As of your last terminal Claude Code reply, 1m ago'],
      ['terminal', NOW - 7200, '2h ago', true, 'As of your last terminal Claude Code reply, 2h ago'],
      ['job', NOW - 7200, '2h ago', true, 'Last recorded usage, 2h ago'],
      [null, null, 'age unknown', true, 'Last recorded usage, age unknown'],
    ] as const) {
      const next = envelope(null, generation++), data = next.usage as typeof usage;
      const account = data.providers[0]!.accounts[0]!;
      next.usage = { ...data, providers: [{ ...data.providers[0]!, accounts: [{ ...account, statusline_usage: 'live',
        windows: [{ ...account.windows[0]!, used_percent: 34, observed_at, source, stale: false }],
      }] }] };
      await status(next);
      const target = row('claude:claude-work')!;
      expect(target.querySelector('.usage-figure-line')?.textContent).toBe('5h34%');
      expect(target.querySelector('.observation-age')?.textContent).toBe(age);
      expect(target.querySelector('.row-slot')?.classList.contains('is-stale')).toBe(stale);
      const spokenSource = `${context[0]!.toLowerCase()}${context.slice(1)}`;
      expect(target.querySelector('.row-link')?.getAttribute('aria-label')).toBe(`Claude claude-work. Available. 5h 34%, ${stale ? 'stale, ' : ''}${spokenSource}, resets 20:00. Show account detail.`);
      expect(target.querySelector('.account-context')?.textContent).toBe(`${context}Settings…`);
    }
  });

  it('click, Return and Space toggle one panel; closed detail is inert, hidden from readers, and closes with the popover', async () => {
    const user = userEvent.setup();
    const { api } = await renderPopover();
    show();
    const link = (key: string) => row(key)!.querySelector<HTMLButtonElement>('.row-link')!;
    const work = link('claude:claude-work');
    const panel = document.getElementById(work.getAttribute('aria-controls')!)!;
    expect(work.getAttribute('aria-expanded')).toBe('false');
    expect(panel.hidden).toBe(true);
    expect(panel.getAttribute('inert')).toBe('');
    fireEvent.pointerOver(work);
    expect(work.getAttribute('aria-expanded')).toBe('false');
    act(() => work.focus());
    expect(screen.queryByRole('tooltip')).toBeNull();
    await user.keyboard('{Enter}');
    expect(work.getAttribute('aria-expanded')).toBe('true');
    expect(panel.hidden).toBe(false);
    expect(panel.getAttribute('aria-hidden')).toBe('false');
    expect(panel.getAttribute('inert')).toBeNull();
    expect(panel.textContent).toContain('5 hours  17%  Resets 20:00 · 1m ago');
    expect(panel.textContent).toContain('Week  86%  Resets Wed 19:00 · 1m ago');
    expect(panel.textContent).toContain('Last recorded usage, 1m ago · Terminal updates not set up');
    await user.keyboard(' ');
    expect(work.getAttribute('aria-expanded')).toBe('false');
    expect(panel.getAttribute('aria-hidden')).toBe('true');
    expect(screen.queryByRole('button', { name: 'Settings for claude-work' })).toBeNull();
    fireEvent.click(work);
    const kiro = link('kiro:primary');
    fireEvent.click(kiro);
    expect(work.getAttribute('aria-expanded')).toBe('false');
    expect(kiro.getAttribute('aria-expanded')).toBe('true');
    expect(row('kiro:primary')!.querySelector('.account-context')?.textContent).toContain('No usage reported · Updated 0m ago');
    fireEvent.click(button('Settings for primary'));
    expect(api.openSettings).toHaveBeenCalledWith({ runtime: 'kiro', account: 'primary' });
    hide();
    expect(kiro.getAttribute('aria-expanded')).toBe('false');
    show();
    expect(kiro.getAttribute('aria-expanded')).toBe('false');
  });
});

describe('trouble and stale data', () => {
  it('keeps values, dims rows, shows their age, and preserves timed returns while Retry says Retrying…', async () => {
    const { api, status } = await renderPopover();
    show();
    const error = 'agt usage --json timed out after 10 seconds.';
    const next = { ...envelope(null, 2), error, fetched_at: NOW - 240 };
    await status(next);
    expect(screen.getByRole('alert').textContent).toBe(`${error} The numbers below are from 4 minutes ago.`);
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe("Can't read agt");
    expect(slot('claude:claude-work')).toBe('wk86%1m ago');
    expect(slot('claude:claude-cooling')).toBe('Back 21:40');
    expect(document.querySelectorAll('#accounts .account-main.is-dimmed')).toHaveLength(accounts.length);
    const retry = button('Retry');
    expect(retry.classList.contains('trouble-action')).toBe(true);
    fireEvent.click(retry);
    expect(api.refreshUsage).toHaveBeenCalledTimes(1);
    await status({ ...next, reading: true, generation: 3 });
    expect(button('Retrying…')).toBe(retry);
    expect(retry.querySelector('.is-turning, .turning-icon')).toBeNull();
    expect(retry.getAttribute('aria-busy')).toBe('true');
    expect(document.querySelectorAll('.is-turning')).toHaveLength(1);
    expect(document.querySelector('#refresh .is-turning')).not.toBeNull();
    fireEvent.click(retry);
    expect(api.refreshUsage).toHaveBeenCalledTimes(1);
    await status({ ...next, reading: false, generation: 4 });
    expect(button('Retry')).toBe(retry);
    expect(document.querySelector('.refresh-feedback')?.getAttribute('data-checked')).toBe('false');
    await status({ ...envelope(null, 5), error: null });
    expect(visible('trouble-row')).toBe(false);
    expect(slot('claude:claude-work')).toBe('wk86%1m ago');
  });

  it('retains the last successful snapshot when a failure envelope has no usage', async () => {
    const { status } = await renderPopover();
    await status({ ...envelope('unreadable', 2), fetched_at: null });
    expect(document.querySelectorAll('#accounts .account-row')).toHaveLength(accounts.length);
    expect(document.querySelectorAll('#accounts .account-main.is-dimmed')).toHaveLength(accounts.length);
    expect(slot('claude:claude-work')).toBe('wk86%1m ago');
  });

  it('2h ago appears on popover stale rows and off rows are dimmed', async () => {
    await renderPopover();
    expect(slot('claude:claude-stale')).toBe('wk86%2h ago');
    expect(row('claude:claude-stale')!.querySelector('.row-slot')?.classList.contains('is-stale')).toBe(true);
    expect(row('claude:claude-off')!.querySelector('.account-main')?.classList.contains('is-dimmed')).toBe(true);
  });
});

describe('opening focus', () => {
  it('drops Refresh focus after show without a press and hides its tooltip', async () => {
    await renderPopover();
    show();
    const refresh = button('Refresh usage');
    act(() => refresh.focus());
    expect(document.activeElement).toBe(document.body);
    expect(refresh.matches(':focus')).toBe(false);
    expect(screen.queryByRole('tooltip')).toBeNull();
  });

  it('drops Refresh focus already set before the window focus event', async () => {
    await renderPopover();
    const refresh = button('Refresh usage');
    act(() => refresh.focus());
    show();
    expect(document.activeElement).toBe(document.body);
    expect(refresh.matches(':focus')).toBe(false);
    expect(screen.queryByRole('tooltip')).toBeNull();
  });

  it.each(['keyboard', 'pointer', 'mouse'] as const)('keeps Refresh focus and its tooltip after a %s press', async (input) => {
    await renderPopover();
    show();
    if (input === 'keyboard') fireEvent.keyDown(document, { key: 'Tab' });
    else if (input === 'pointer') fireEvent.pointerDown(document.body);
    else fireEvent.mouseDown(document.body);
    const refresh = button('Refresh usage');
    act(() => refresh.focus());
    await act(async () => { await Promise.resolve(); });
    expect(document.activeElement).toBe(refresh);
    expect(refresh.matches(':focus')).toBe(true);
    expect(screen.getByRole('tooltip').hidden).toBe(false);
  });

  it('keeps VoiceOver focus without a page press once opening has ended', async () => {
    vi.useFakeTimers();
    try {
      await renderPopover();
      show();
      act(() => vi.advanceTimersByTime(cssTime('--panel-time') + 1));
      const refresh = button('Refresh usage');
      act(() => refresh.focus());
      await act(async () => { await Promise.resolve(); });
      expect(document.activeElement).toBe(refresh);
      expect(refresh.matches(':focus')).toBe(true);
      expect(screen.getByRole('tooltip').hidden).toBe(false);
    } finally { vi.useRealTimers(); }
  });

  it('drops automatic Refresh focus again after opening ended, hide and reopen', async () => {
    vi.useFakeTimers();
    try {
      await renderPopover();
      show();
      act(() => vi.advanceTimersByTime(cssTime('--panel-time') + 1));
      hide();
      show();
      const refresh = button('Refresh usage');
      act(() => refresh.focus());
      expect(document.activeElement).toBe(document.body);
      expect(refresh.matches(':focus')).toBe(false);
      expect(screen.queryByRole('tooltip')).toBeNull();
    } finally { vi.useRealTimers(); }
  });

  it('drops opening focus even when no focusin reaches the document listeners', async () => {
    const stopFocusIn = (event: FocusEvent) => event.stopImmediatePropagation();
    document.addEventListener('focusin', stopFocusIn, true);
    try {
      await renderPopover();
      show();
      const refresh = button('Refresh usage');
      act(() => refresh.focus());
      expect(document.activeElement).toBe(document.body);
      expect(screen.queryByRole('tooltip')).toBeNull();
    } finally { document.removeEventListener('focusin', stopFocusIn, true); }
  });

  it('restarts the full opening interval after hide and show', async () => {
    vi.useFakeTimers();
    try {
      await renderPopover();
      show();
      const half = cssTime('--panel-time') / 2;
      act(() => vi.advanceTimersByTime(half));
      hide();
      show();
      act(() => vi.advanceTimersByTime(half + 1));
      const refresh = button('Refresh usage');
      act(() => refresh.focus());
      expect(document.activeElement).toBe(document.body);
      act(() => vi.advanceTimersByTime(half));
      act(() => refresh.focus());
      await act(async () => { await Promise.resolve(); });
      expect(document.activeElement).toBe(refresh);
      expect(screen.getByRole('tooltip').hidden).toBe(false);
    } finally { vi.useRealTimers(); }
  });

  it.each(['focus', 'visibilitychange'] as const)('does not extend opening after a repeated %s notification', async (notification) => {
    vi.useFakeTimers();
    try {
      await renderPopover();
      show();
      const duration = cssTime('--panel-time');
      act(() => vi.advanceTimersByTime(duration * 2 / 3));
      if (notification === 'focus') show();
      else {
        vi.spyOn(document, 'hidden', 'get').mockReturnValue(false);
        vi.spyOn(document, 'hasFocus').mockReturnValue(true);
        act(() => document.dispatchEvent(new Event('visibilitychange')));
      }
      act(() => vi.advanceTimersByTime(duration / 3 + 1));
      const refresh = button('Refresh usage');
      act(() => refresh.focus());
      expect(document.activeElement).toBe(refresh);
      expect(screen.getByRole('tooltip').hidden).toBe(false);
    } finally { vi.useRealTimers(); }
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
    expect(document.activeElement).toBe(button('Install and Restart'));
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
    expect(document.activeElement?.getAttribute('aria-expanded')).toBe('true');
    expect(api.openSettings).not.toHaveBeenCalled();
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

  it('Escape hides the tooltip and the popover when no native menu is tracking', async () => {
    const { api } = await renderPopover();
    fireEvent.keyDown(document, { key: 'Tab' });
    act(() => button('Refresh usage').focus());
    expect(screen.getByRole('tooltip').hidden).toBe(false);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.getByRole('tooltip', { hidden: true }).hidden).toBe(true);
    expect(api.hidePopover).toHaveBeenCalledTimes(1);
  });
});

describe('native More menu', () => {
  it('keeps Refresh focus after native menu blur and refocus without a new page press', async () => {
    vi.useFakeTimers();
    try {
      const { api } = await renderPopover();
      show();
      let dismiss!: () => void;
      api.showPopoverMenu.mockReturnValueOnce(new Promise<void>((resolve) => { dismiss = resolve; }));
      const more = button('More');
      fireEvent.pointerDown(more);
      fireEvent.click(more);
      expect(api.showPopoverMenu).toHaveBeenCalledTimes(1);
      hide();
      show();
      await act(async () => dismiss());
      const refresh = button('Refresh usage');
      act(() => refresh.focus());
      expect(document.activeElement).toBe(refresh);
      expect(screen.getByRole('tooltip').hidden).toBe(false);
    } finally { vi.useRealTimers(); }
  });

  it('rearms opening when the native menu finishes while the panel is hidden', async () => {
    vi.useFakeTimers();
    try {
      const { api } = await renderPopover();
      show();
      let dismiss!: () => void;
      api.showPopoverMenu.mockReturnValueOnce(new Promise<void>((resolve) => { dismiss = resolve; }));
      const more = button('More');
      fireEvent.pointerDown(more);
      fireEvent.click(more);
      hide();
      expect(document.body.dataset.active).toBe('false');
      const refresh = button('Refresh usage');
      act(() => refresh.focus());
      expect(document.activeElement).toBe(refresh);
      await act(async () => dismiss());
      expect(document.body.dataset.active).toBe('false');
      show();
      act(() => refresh.focus());
      expect(document.activeElement).toBe(document.body);
      expect(screen.queryByRole('tooltip')).toBeNull();
    } finally { vi.useRealTimers(); }
  });

  it('anchors under the button rect in logical coordinates without an in-page menu', async () => {
    const { api } = await renderPopover();
    const more = button('More');
    vi.spyOn(more, 'getBoundingClientRect').mockReturnValue(new DOMRect(308.5, 4, 24, 24));
    await act(async () => { fireEvent.click(more); });
    expect(api.showPopoverMenu).toHaveBeenCalledWith({ x: 308.5, y: 28 });
    expect(screen.queryByRole('menu', { hidden: true })).toBeNull();
    expect(document.getElementById('app-menu')).toBeNull();
    expect(more.hasAttribute('aria-controls')).toBe(false);
    expect(popoverStyles).not.toContain('.app-menu');
    expect(document.activeElement).toBe(more);
  });

  it('waits for native dismissal, ignores repeat presses and restores More focus', async () => {
    const { api } = await renderPopover();
    show();
    let dismiss!: () => void;
    api.showPopoverMenu.mockReturnValueOnce(new Promise<void>(resolve => { dismiss = resolve; }));
    const more = button('More');
    fireEvent.click(more);
    fireEvent.click(more);
    expect(api.showPopoverMenu).toHaveBeenCalledTimes(1);
    expect(more.getAttribute('aria-expanded')).toBe('true');
    fireEvent.keyDown(document, { key: 'Tab' });
    act(() => button('Refresh usage').focus());
    // The real native menu consumes Escape before WebKit. A duplicate key
    // delivered to WebKit while the promise is pending must not hide the panel.
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(api.hidePopover).not.toHaveBeenCalled();
    await act(async () => { dismiss(); });
    expect(more.getAttribute('aria-expanded')).toBe('false');
    expect(document.activeElement).toBe(more);
    expect(document.body.dataset.active).toBe('true');
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(api.hidePopover).toHaveBeenCalledTimes(1);
  });

  it('a native menu rejection keeps usage readable and focus on More without a trouble row', async () => {
    const { api } = await renderPopover();
    api.showPopoverMenu.mockRejectedValueOnce(new Error('native menu unavailable'));
    const heading = document.getElementById('heading')!.textContent;
    await act(async () => { fireEvent.click(button('More')); });
    expect(visible('trouble-row')).toBe(false);
    expect(document.getElementById('heading')!.textContent).toBe(heading);
    expect(document.activeElement).toBe(button('More'));
    expect(button('More').getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByRole('menu', { hidden: true })).toBeNull();
  });
});

describe('inline actions', () => {
  const loggedOut = () => row('claude:claude-logged-out')!;
  const copyLogin = () => within(loggedOut()).getByRole('button', { name: 'Copy login command for claude-logged-out' });

  it('exposes the account-specific copy name only on login rows', async () => {
    await renderPopover();
    const copy = copyLogin();
    expect(copy.getAttribute('data-tooltip')).toBe('Copy login command');
    expect(loggedOut().querySelector('.status-dot')?.getAttribute('data-shape')).toBe('ring');
    expect(loggedOut().querySelector('.row-action')?.textContent).toBe('Log in…');
    expect(row('claude:claude-work')!.querySelector('.row-copy')).toBeNull();
  });

  it('reveals the copy icon on row hover and focus-within while keeping its hit target', async () => {
    await renderPopover();
    const copy = copyLogin();
    // jsdom does not paint dynamic pseudo-classes. Inspect the real stylesheet
    // declarations and their targets, then exercise the focus-within trigger.
    const sheet = new CSSStyleSheet();
    sheet.replaceSync(popoverStyles);
    const rules = [...sheet.cssRules].filter((rule): rule is CSSStyleRule => 'selectorText' in rule);
    const initial = rules.find((rule) => rule.selectorText === '.popover-account .row-copy')!;
    expect(copy.matches(initial.selectorText)).toBe(true);
    expect(initial.style.opacity).toBe('0');
    expect(initial.style.getPropertyValue('flex')).toBe('0 0 var(--hit)');
    expect(initial.style.getPropertyValue('transition')).toContain('var(--hover-time)');
    for (const state of [':hover', ':focus-within']) {
      const reveal = rules.find((rule) => rule.selectorText.includes(`.popover-account${state} .row-copy`))!;
      const selector = reveal.selectorText.split(',').find((part) => part.includes(state))!;
      expect(copy.matches(selector.replace(state, ''))).toBe(true);
      expect(reveal.style.opacity).toBe('1');
      expect(reveal.style.getPropertyValue('pointer-events')).toBe('auto');
    }
    act(() => (loggedOut().querySelector('.row-action') as HTMLElement).focus());
    expect(loggedOut().contains(document.activeElement)).toBe(true);
  });

  it('reaches copy as the third roving part after row and action, and preserves it across refreshes', async () => {
    const { fresh } = await renderPopover();
    const link = loggedOut().querySelector<HTMLButtonElement>('.row-link')!;
    const login = loggedOut().querySelector<HTMLButtonElement>('.row-action')!;
    act(() => link.focus());
    fireEvent.keyDown(link, { key: 'ArrowRight' });
    expect(document.activeElement).toBe(login);
    fireEvent.keyDown(login, { key: 'ArrowRight' });
    const copy = copyLogin();
    expect(document.activeElement).toBe(copy);
    expect(copy.tabIndex).toBe(0);
    expect(link.tabIndex).toBe(-1);
    expect(login.tabIndex).toBe(-1);
    expect(document.querySelectorAll('#accounts button[tabindex="0"]')).toHaveLength(1);
    await fresh(envelope(null, 2));
    expect(document.activeElement).toBe(copyLogin());
    fireEvent.keyDown(copy, { key: 'ArrowLeft' });
    expect(document.activeElement).toBe(login);
  });

  it('copies the Rust command without login, announces Copied and holds the cross-faded check', async () => {
    vi.useFakeTimers();
    try {
      const { api } = await renderPopover();
      const command = "'a launcher' 'accounts' 'login' 'claude-logged-out'";
      api.getLoginCommand.mockResolvedValue(command);
      const copy = copyLogin();
      await act(async () => { fireEvent.click(copy); });
      expect(api.getLoginCommand).toHaveBeenCalledWith('claude', 'claude-logged-out');
      expect(api.copyText).toHaveBeenCalledWith(command);
      expect(api.loginAccount).not.toHaveBeenCalled();
      expect(document.getElementById('announcement')?.textContent).toBe('Copied');
      const feedback = copy.querySelector('.copy-feedback')!;
      expect(feedback.getAttribute('data-copied')).toBe('true');
      expect(feedback.querySelectorAll('.icon')).toHaveLength(3);
      expect(feedback.querySelector('.copy-glyph')).not.toBeNull();
      expect(feedback.querySelector('.copy-check')).not.toBeNull();
      expect(feedback.querySelector('.copy-alert')).not.toBeNull();
      expect(sharedStyles).toContain('transition: opacity var(--cross-fade-time) var(--ease-out)');
      act(() => vi.advanceTimersByTime(cssTime('--copied-delay') - 1));
      expect(feedback.getAttribute('data-copied')).toBe('true');
      act(() => vi.advanceTimersByTime(1));
      expect(feedback.getAttribute('data-copied')).toBe('false');
    } finally { vi.useRealTimers(); }
  });

  it('opens Terminal-failed detail and copies its returned command with a Copied fallback', async () => {
    const { api } = await renderPopover();
    show();
    const command = 'the exact Rust fallback command';
    api.loginAccount.mockResolvedValueOnce({ status: 'terminal_failed', copy_command: command });
    await act(async () => { fireEvent.click(loggedOut().querySelector('.row-action')!); });
    expect(loggedOut().querySelector('.row-link')?.getAttribute('aria-expanded')).toBe('true');
    expect(within(loggedOut()).getByText("Couldn't open Terminal.")).toBeTruthy();
    hide();
    show();
    // Hiding closes detail; reopening that row retains the Terminal fallback.
    fireEvent.click(loggedOut().querySelector('.row-link')!);
    expect(within(loggedOut()).getByText("Couldn't open Terminal.")).toBeTruthy();
    const fallback = within(loggedOut()).getByRole('button', { name: 'Copy command' });
    act(() => fallback.focus());
    fireEvent.keyDown(fallback, { key: 'ArrowRight' });
    expect(document.activeElement).toBe(fallback);
    await act(async () => { fireEvent.click(fallback); });
    expect(api.copyText).toHaveBeenCalledWith(command);
    expect(api.getLoginCommand).not.toHaveBeenCalled();
    expect(within(loggedOut()).getByRole('button', { name: 'Copied' })).toBe(fallback);
    expect(document.getElementById('announcement')?.textContent).toBe('Copied');
    // Login remains available; a successful handoff clears its earlier failure.
    await act(async () => { fireEvent.click(loggedOut().querySelector('.row-action')!); });
    expect(within(loggedOut()).queryByText("Couldn't open Terminal.")).toBeNull();
  });

  it.each(['getLoginCommand', 'copyText'] as const)('%s rejection cross-fades the copy alert for --copied-delay without a trouble row', async (method) => {
    vi.useFakeTimers();
    try {
      const { api } = await renderPopover();
      show();
      const heading = document.getElementById('heading')!.textContent;
      api[method].mockRejectedValueOnce(new Error('Copy failed internally'));
      const copy = copyLogin();
      act(() => copy.focus());
      await act(async () => { fireEvent.click(copy); });
      if (method === 'getLoginCommand') expect(api.copyText).not.toHaveBeenCalled();
      expect(document.activeElement).toBe(copy);
      expect(copy.querySelector('.copy-feedback')?.getAttribute('data-failed')).toBe('true');
      expect(copy.querySelector('.copy-feedback')?.getAttribute('data-copied')).toBe('false');
      expect(copy.querySelector('.copy-alert')).not.toBeNull();
      expect(copy.getAttribute('data-tooltip')).toBe("Couldn't copy.");
      expect(screen.getByRole('tooltip').textContent).toBe("Couldn't copy.");
      expect(document.getElementById('announcement')?.textContent).toBe("Couldn't copy.");
      expect(document.getElementById('heading')!.textContent).toBe(heading);
      expect(visible('trouble-row')).toBe(false);
      expect(screen.queryByRole('alert')).toBeNull();
      expect(loggedOut().querySelector('.row-link')?.getAttribute('aria-expanded')).toBe('false');
      expect(sharedStyles).toContain('.copy-feedback[data-failed="true"] .copy-alert { opacity: 1; }');
      expect(sharedStyles).toContain('transition: opacity var(--cross-fade-time) var(--ease-out)');
      act(() => vi.advanceTimersByTime(cssTime('--copied-delay') - 1));
      expect(copy.querySelector('.copy-feedback')?.getAttribute('data-failed')).toBe('true');
      await act(async () => { vi.advanceTimersByTime(1); });
      expect(copy.querySelector('.copy-feedback')?.getAttribute('data-failed')).toBe('false');
      expect(copy.getAttribute('data-tooltip')).toBe('Copy login command');
      expect(screen.getByRole('tooltip').textContent).toBe('Copy login command');
      await act(async () => { fireEvent.click(copy); });
      expect(copy.querySelector('.copy-feedback')?.getAttribute('data-copied')).toBe('true');
    } finally { vi.useRealTimers(); }
  });

  it('Copy command failure holds its label and announcement for --copied-delay and retries on press', async () => {
    vi.useFakeTimers();
    try {
      const { api } = await renderPopover();
      show();
      api.loginAccount.mockResolvedValueOnce({ status: 'terminal_failed', copy_command: 'Rust fallback' });
      await act(async () => { fireEvent.click(loggedOut().querySelector('.row-action')!); });
      const fallback = within(loggedOut()).getByRole('button', { name: 'Copy command' });
      act(() => fallback.focus());
      api.copyText.mockRejectedValueOnce(new Error('Pasteboard unavailable'));
      await act(async () => { fireEvent.click(fallback); });
      expect(fallback.textContent).toBe("Couldn't copy");
      expect(fallback.getAttribute('data-tooltip')).toBe("Couldn't copy.");
      expect(document.getElementById('announcement')?.textContent).toBe("Couldn't copy.");
      expect(document.activeElement).toBe(fallback);
      expect(visible('trouble-row')).toBe(false);
      act(() => vi.advanceTimersByTime(cssTime('--copied-delay') - 1));
      expect(fallback.textContent).toBe("Couldn't copy");
      act(() => vi.advanceTimersByTime(1));
      expect(fallback.textContent).toBe('Copy command');
      api.copyText.mockRejectedValueOnce(new Error('Still unavailable'));
      await act(async () => { fireEvent.click(fallback); });
      expect(fallback.textContent).toBe("Couldn't copy");
      await act(async () => { fireEvent.click(fallback); });
      expect(fallback.textContent).toBe('Copied');
      expect(api.copyText).toHaveBeenLastCalledWith('Rust fallback');
    } finally { vi.useRealTimers(); }
  });

  it('login rejection opens a described row line with only the raw error in its tooltip and keeps focus', async () => {
    const { api } = await renderPopover();
    show();
    const heading = document.getElementById('heading')!.textContent;
    const login = loggedOut().querySelector<HTMLButtonElement>('.row-action')!;
    act(() => login.focus());
    api.loginAccount.mockRejectedValueOnce(new Error('Unknown account'));
    await act(async () => { fireEvent.click(login); });
    const line = within(loggedOut()).getByText("Couldn't start login.");
    expect(line.getAttribute('data-tooltip')).toBe('Error: Unknown account');
    expect(login.getAttribute('aria-describedby')?.split(' ')).toContain(line.id);
    expect(document.activeElement).toBe(login);
    expect(document.getElementById('announcement')?.textContent).toBe("Couldn't start login.");
    expect(loggedOut().querySelector('.row-link')?.getAttribute('aria-expanded')).toBe('true');
    expect(within(loggedOut()).queryByRole('button', { name: 'Copy command' })).toBeNull();
    expect(document.getElementById('heading')!.textContent).toBe(heading);
    expect(visible('trouble-row')).toBe(false);
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it.each(['opened', 'terminal_failed'] as const)('clears a login rejection on the next press before %s replaces it', async (status) => {
    const { api } = await renderPopover();
    show();
    const login = loggedOut().querySelector<HTMLButtonElement>('.row-action')!;
    api.loginAccount.mockRejectedValueOnce(new Error('Rejected'));
    await act(async () => { fireEvent.click(login); });
    let finish = () => {};
    api.loginAccount.mockImplementationOnce(() => new Promise((resolve) => {
      finish = () => resolve({ status, copy_command: 'Returned command' });
    }));
    fireEvent.click(login);
    expect(within(loggedOut()).queryByText("Couldn't start login.")).toBeNull();
    expect(login.getAttribute('aria-describedby')).toBeNull();
    await act(async () => finish());
    expect(within(loggedOut()).queryByText("Couldn't start login.")).toBeNull();
    expect(!!within(loggedOut()).queryByText("Couldn't open Terminal.")).toBe(status === 'terminal_failed');
  });

  it('clears login rejections on close and row removal, including late replies after close', async () => {
    const { api, fresh } = await renderPopover();
    show();
    const reject = async () => {
      api.loginAccount.mockRejectedValueOnce(new Error('Rejected'));
      await act(async () => { fireEvent.click(loggedOut().querySelector('.row-action')!); });
      expect(within(loggedOut()).getByText("Couldn't start login.")).toBeTruthy();
    };
    await reject();
    hide();
    show();
    expect(within(loggedOut()).queryByText("Couldn't start login.")).toBeNull();
    expect(loggedOut().querySelector('.row-action')?.getAttribute('aria-describedby')).toBeNull();
    await reject();
    const next = envelope(null, 2);
    const data = next.usage as typeof usage;
    next.usage = { ...data, providers: data.providers.map((provider) => ({ ...provider,
      accounts: provider.accounts.filter((account) => account.name !== 'claude-logged-out') })) };
    await fresh(next);
    expect(row('claude:claude-logged-out')).toBeNull();
    await fresh(envelope(null, 3));
    expect(within(loggedOut()).queryByText("Couldn't start login.")).toBeNull();
    let fail = (_error: Error) => {};
    api.loginAccount.mockImplementationOnce(() => new Promise((_resolve, reject) => { fail = reject; }));
    fireEvent.click(loggedOut().querySelector('.row-action')!);
    hide();
    show();
    await act(async () => fail(new Error('Late rejection')));
    expect(within(loggedOut()).queryByText("Couldn't start login.")).toBeNull();
    expect(loggedOut().querySelector('.row-link')?.getAttribute('aria-expanded')).toBe('false');
  });

  it('retains copy confirmation as a cross-fade with reduced motion', async () => {
    media.setReduced(true);
    const { api } = await renderPopover();
    await act(async () => { fireEvent.click(copyLogin()); });
    expect(api.copyText).toHaveBeenCalledTimes(1);
    expect(copyLogin().querySelector('.copy-feedback')?.getAttribute('data-copied')).toBe('true');
    expect(document.getElementById('announcement')?.textContent).toBe('Copied');
  });

  it('Log in shows its spinner only after --terminal-delay and ignores repeat clicks', async () => {
    const { api } = await renderPopover();
    let finish = () => {};
    api.loginAccount.mockImplementation(() => new Promise((resolve) => { finish = () => resolve({ status: 'opened', copy_command: 'fake login command' }); }));
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

  it('never-checked popover rows show Checking… without a Check button or webview-triggered checks', async () => {
    const { api } = await renderPopover();
    expect(row('claude:claude-unknown')!.querySelector('.row-action')).toBeNull();
    expect(slot('claude:claude-unknown')).toBe('Checking…');
    expect(api.checkAccount).not.toHaveBeenCalled();
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

  it('only new rows stagger by --stagger-step, capped at 8 rows; refreshing existing rows never staggers', async () => {
    const { fresh } = await renderPopover(envelope(null), true);
    show();
    await fresh(envelope(null, 2));
    expect(document.querySelectorAll('#accounts .row-enter')).toHaveLength(0);
    const next = envelope(null, 3), data = next.usage as typeof usage;
    const template = data.providers[0]!.accounts[0]!;
    const additions = Array.from({ length: 10 }, (_, index) => ({ ...template, name: `added-${index}` }));
    next.usage = { ...data, providers: data.providers.map((provider, index) => index === 0
      ? { ...provider, accounts: [...provider.accounts, ...additions] } : provider) };
    await fresh(next);
    expect(cssTime('--stagger-step')).toBe(24);
    expect(row('claude:claude-work')!.classList.contains('row-enter')).toBe(false);
    for (let index = 0; index < additions.length; index += 1) {
      const item = row(`claude:added-${index}`)!;
      expect(item.classList.contains('row-enter')).toBe(true);
      expect(Number(item.style.getPropertyValue('--i')) * cssTime('--stagger-step')).toBe(Math.min(index, 7) * 24);
    }
    hide();
    show();
    expect(document.querySelectorAll('#accounts .row-enter')).toHaveLength(0);
    await fresh({ ...structuredClone(next), generation: 4 });
    expect(document.querySelectorAll('#accounts .row-enter')).toHaveLength(0);
    expect(document.querySelectorAll('#accounts .gauge-bar.is-changing, #accounts .figure-strip')).toHaveLength(0);
  });

  it('fresh usage springs only the changed bar and rolls only the changed digit, without staggering', async () => {
    const { fresh } = await renderPopover();
    show();
    await fresh(envelope(null, 2));
    expect(document.querySelectorAll('#accounts .gauge-bar.is-changing, #accounts .figure-strip')).toHaveLength(0);
    await fresh(bumped(3, 88));
    const work = row('claude:claude-work')!;
    expect(work.querySelectorAll('.gauge-bar.is-changing')).toHaveLength(1);
    expect(work.querySelector('.gauge-bar.is-changing')?.getAttribute('data-window')).toBe('seven_day');
    expect(work.querySelectorAll('.figure-strip')).toHaveLength(1);
    expect(work.querySelector('.figure-strip')?.textContent).toBe('68');
    expect(document.querySelectorAll('#accounts .row-enter')).toHaveLength(0);
    fireEvent.animationEnd(work.querySelector('.figure-strip')!);
    await fresh(bumped(4, 88));
    expect(document.querySelectorAll('#accounts .gauge-bar.is-changing, #accounts .figure-strip')).toHaveLength(0);
  });

  it('Refresh spins while reading, then holds a cross-faded check for --copied-delay', async () => {
    vi.useFakeTimers();
    try {
      const { status, fresh } = await renderPopover();
      show();
      expect(button('Refresh usage').getAttribute('data-tooltip')).toBe('Refresh ⌘R · Read 0m ago');
      fireEvent.click(button('Refresh usage'));
      await status(envelope('reading', 2));
      expect(document.querySelector('#refresh .is-turning')).not.toBeNull();
      expect(document.querySelector('.refresh-feedback')?.getAttribute('data-checked')).toBe('false');
      await fresh(envelope(null, 3));
      expect(document.querySelector('.refresh-feedback')?.getAttribute('data-checked')).toBe('true');
      act(() => vi.advanceTimersByTime(cssTime('--copied-delay') - 1));
      expect(document.querySelector('.refresh-feedback')?.getAttribute('data-checked')).toBe('true');
      act(() => vi.advanceTimersByTime(1));
      expect(document.querySelector('.refresh-feedback')?.getAttribute('data-checked')).toBe('false');
    } finally { vi.useRealTimers(); }
  });

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
    expect(work.querySelector('.gauge-bar.is-changing')).toBeNull();
  });

  it('reduced motion uses plain Retrying… instead of a spinner', async () => {
    media.setReduced(true);
    await renderPopover({ ...envelope('unreadable'), reading: true });
    const retry = button('Retrying…');
    expect(retry.textContent).toBe('Retrying…');
    expect(retry.querySelector('.turning-icon')).toBeNull();
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
    const { api, unlistened, unmount, update } = await renderPopover();
    await act(async () => { await Promise.resolve(); });
    // Loading, then the first answer (header and list): both snap.
    const sizes = () => api.setPopoverHeight.mock.calls.map(([size]) => size);
    expect(sizes().every((size) => size.durationMs === 0)).toBe(true);
    expect(sizes().at(-1)?.height).toBe(40);
    // A later change (the update footer appears) animates with --panel-resize-time.
    await update(updateStatus('0.17.1'));
    await act(async () => { await Promise.resolve(); });
    expect(sizes().at(-1)).toMatchObject({ height: 60, durationMs: cssTime('--panel-resize-time') });
    const observer = resizeObservers.at(-1)!;
    expect(observer.observed.size).toBe(6);
    unmount();
    expect(unlistened.sort()).toEqual(['account-checks-status', 'popover-status', 'update-status', 'usage-updated']);
    expect(observer.disconnected).toBe(true);
    fireEvent.keyDown(document, { key: 'Escape' });
    fireEvent.keyDown(document, { key: 'q', metaKey: true });
    expect(api.hidePopover).not.toHaveBeenCalled();
    expect(api.quitApp).not.toHaveBeenCalled();
  });
});

describe('S9/S10 one-click update', () => {
  const footer = () => document.getElementById('last-row')!;
  const footerText = () => footer().querySelector('.update-text')?.textContent;
  const never = () => new Promise<void>(() => {});
  // Every api mock's call count, and the counts that changed between two readings.
  const calls = (api: ReturnType<typeof fakeApi>['api']) =>
    Object.fromEntries(Object.entries(api).map(([name, mock]) => [name, mock.mock.calls.length]));
  const moved = (before: Record<string, number>, after: Record<string, number>) =>
    Object.fromEntries(Object.entries(after).filter(([name, n]) => n !== before[name]).map(([name, n]) => [name, n - (before[name] ?? 0)]));

  it('one click shows spinner plus Installing… and two rapid clicks start one install', async () => {
    const { api } = await renderPopover(envelope('update'));
    api.installUpdate.mockImplementation(never);
    const install = button('Install and Restart');
    // Both clicks reach the handler before React commits the first.
    act(() => { install.click(); install.click(); });
    expect(api.installUpdate).toHaveBeenCalledTimes(1);
    expect(footerText()).toBe('Installing…');
    expect(footer().querySelector('.turning-icon')).not.toBeNull();
    expect(footer().querySelector('.update-dot')).toBeNull();
    expect(footer().querySelector('button')).toBeNull();
    expect(statusText()).toBe('6 ready. Installing…');
    expect(api.openSettings).not.toHaveBeenCalled();
  });

  it('a click while Settings installs starts nothing new: the row follows the shared status', async () => {
    const { api, update } = await renderPopover(envelope('update'));
    api.installUpdate.mockRejectedValue('An update is already installing.');
    // The popover still shows the offer when Settings' install starts.
    fireEvent.click(button('Install and Restart'));
    await update(updateStatus('0.17.1', { phase: 'downloading' }));
    await act(async () => { await Promise.resolve(); });
    expect(api.installUpdate).toHaveBeenCalledTimes(1);
    expect(footerText()).toBe('Installing…');
    expect(footer().querySelector('button')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(api.openSettings).not.toHaveBeenCalled();
  });

  it('Settings\' own install shows Installing… with no button in a popover that never clicked', async () => {
    const { api } = await renderPopover(envelope(null), false, updateStatus('0.17.1', { phase: 'installing' }));
    expect(footerText()).toBe('Installing…');
    expect(footer().querySelector('button')).toBeNull();
    expect(api.installUpdate).not.toHaveBeenCalled();
    expect(api.openSettings).not.toHaveBeenCalled();
  });

  it.each(['downloading', 'installing'] as const)('keeps the footer as the only moving loader while reading during %s', async (phase) => {
    const { update } = await renderPopover(envelope('reading'), false, updateStatus('0.17.1', { phase }));
    show();
    expect(footerText()).toBe('Installing…');
    expect(document.querySelectorAll('.is-turning')).toHaveLength(1);
    expect(footer().querySelector('.is-turning')).not.toBeNull();
    expect(document.querySelector('#refresh .is-turning')).toBeNull();
    expect(button('Refreshing usage').getAttribute('aria-busy')).toBe('true');
    await update(updateStatus(null));
    expect(document.querySelectorAll('.is-turning')).toHaveLength(1);
    expect(document.querySelector('#refresh .is-turning')).not.toBeNull();
  });

  it('progress events show received of total without announcing each chunk', async () => {
    const { api, update } = await renderPopover(envelope('update'));
    fireEvent.click(button('Install and Restart'));
    const progress = () => footer().querySelector('.update-progress')?.textContent;
    await update(updateStatus('0.17.1', { phase: 'downloading', received: 12_340_000, total: 45_600_000 }));
    expect([footerText(), progress()]).toEqual(['Installing…', '12.3 of 45.6 MB']);
    await update(updateStatus('0.17.1', { phase: 'downloading', received: 30_000_000, total: 45_600_000 }));
    expect(progress()).toBe('30.0 of 45.6 MB');
    expect(statusText()).toBe('6 ready. Installing…');
    await update(updateStatus('0.17.1', { phase: 'installing' }));
    expect([footerText(), progress()]).toEqual(['Installing…', '']);
    expect(footer().querySelector('button')).toBeNull();
    expect(api.openSettings).not.toHaveBeenCalled();
  });

  it.each(failures)('$phase, retryable $retryable: $title with $button; detail only in the tooltip', async ({ phase, retryable, title, button: name }) => {
    const detail = 'error sending request for url (https://github.com/releases/latest.json)';
    const { api, update } = await renderPopover(envelope('update'));
    api.installUpdate.mockImplementation(never);
    fireEvent.click(button('Install and Restart'));
    await update(updateStatus('0.17.1', { phase: phase as 'download-failed' | 'install-failed', retryable, detail }));
    expect(footerText()).toBe(title);
    expect(footer().querySelector('.update-dot, .turning-icon')).toBeNull();
    const action = button(name);
    expect(action.classList.contains('button-inline')).toBe(true);
    expect(footer().dataset.tooltip).toBe(detail);
    expect(action.getAttribute('aria-describedby')?.split(' ')).toContain('update-detail');
    const described = document.getElementById('update-detail')!;
    expect([described.textContent, described.hidden]).toEqual([detail, true]);
    // Nothing shown inline carries the detail.
    const inline = footer().cloneNode(true) as HTMLElement;
    inline.querySelectorAll('[hidden]').forEach((node) => node.remove());
    expect(inline.textContent).toBe(`${title}${name}`);
    expect(statusText()).toBe(`6 ready. ${title}`);
    // Keyboard focus shows the detail as the row's tooltip at once.
    act(() => { action.focus(); });
    expect(screen.getByRole('tooltip').textContent).toBe(detail);
    // Retry runs the same command at once, though the first call never settled.
    const before = calls(api);
    fireEvent.click(action);
    await act(async () => { await Promise.resolve(); });
    expect(api.installUpdate).toHaveBeenCalledTimes(retryable ? 2 : 1);
    expect(api.openReleases).toHaveBeenCalledTimes(retryable ? 0 : 1);
    // The click makes one call and touches no other api: no Settings, refresh, login, copy or menu.
    expect(moved(before, calls(api))).toEqual(retryable ? { installUpdate: 1 } : { openReleases: 1 });
    if (retryable) expect(footerText()).toBe('Installing…');
    else expect(footerText()).toBe(title);
    expect(api.openSettings).not.toHaveBeenCalled();
  });

  it('a rejected install with no status frees the button: the next click installs once more', async () => {
    const { api } = await renderPopover(envelope('update'));
    api.installUpdate.mockRejectedValueOnce('The updater is unavailable.');
    fireEvent.click(button('Install and Restart'));
    expect(footerText()).toBe('Installing…');
    // The rejection settles, and no update-status event follows.
    await act(async () => { await Promise.resolve(); });
    expect(footerText()).toBe('0.17.1 is available');
    api.installUpdate.mockImplementation(never);
    const again = button('Install and Restart');
    // Both clicks reach the handler before React commits the first.
    act(() => { again.click(); again.click(); });
    expect(api.installUpdate).toHaveBeenCalledTimes(2);
    expect(footerText()).toBe('Installing…');
    expect(api.openSettings).not.toHaveBeenCalled();
  });

  it('nothing to install hides the row, and the next offer replaces a failure', async () => {
    const { api, update } = await renderPopover(envelope('update'));
    fireEvent.click(button('Install and Restart'));
    await act(async () => { await Promise.resolve(); });
    await update(updateStatus(null));
    expect(visible('last-row')).toBe(false);
    await update(updateStatus('0.17.1', { phase: 'install-failed', retryable: true, detail: 'disk full' }));
    expect(footerText()).toBe("Couldn't install 0.17.1.");
    await update(updateStatus('0.17.2'));
    expect(footerText()).toBe('0.17.2 is available');
    expect(footer().dataset.tooltip).toBeUndefined();
    expect(document.getElementById('update-detail')).toBeNull();
    expect(api.openSettings).not.toHaveBeenCalled();
  });

  it('Reduce Motion keeps Installing… and removes the spinner; the button keeps the shared focus outline', async () => {
    media.setReduced(true);
    const { update } = await renderPopover({ ...envelope('update'), reading: true });
    show();
    const install = button('Install and Restart');
    expect(install.classList.contains('button')).toBe(true);
    expect(sharedStyles).toMatch(/\.button:focus-visible \{ outline: var\(--focus-ring\)/);
    await update(updateStatus('0.17.1', { phase: 'downloading' }));
    expect(footerText()).toBe('Installing…');
    expect(footer().querySelector('.turning-icon')).toBeNull();
    expect(document.querySelectorAll('.is-turning')).toHaveLength(0);
    expect(button('Refreshing usage').getAttribute('aria-busy')).toBe('true');
  });
});

describe('S6 shared account-check controller', () => {
  it('shows a static checking bar, then Retry after failure, and Log in… after a logged-out success', async () => {
    const { api, checks } = await renderPopover();
    show();
    const key = 'claude:claude-unknown';
    const snapshot = { runtime: 'claude', account: 'claude-unknown', phase: 'checking' as const, result: null };
    checks([snapshot]);
    expect(slot(key)).toBe('Checking…');
    const checking = within(row(key)!).getByRole('status', { name: 'Checking claude-unknown' });
    expect(checking.querySelector('.skeleton-bar')?.getAttribute('aria-hidden')).toBe('true');
    expect(checking.querySelector('.visually-hidden')?.textContent).toBe('Checking…');
    const visibleSlot = row(key)!.querySelector('.row-slot')!.cloneNode(true) as HTMLElement;
    visibleSlot.querySelectorAll('.visually-hidden').forEach((node) => node.remove());
    expect(visibleSlot.textContent).toBe('');
    expect(row(key)!.querySelector('.turning-icon')).toBeNull();
    expect(row(key)!.querySelector('.status-dot')?.getAttribute('data-shape')).toBe('hollow');
    expect(row(key)!.querySelector('.row-action')).toBeNull();
    checks([{ ...snapshot, phase: 'failed', result: { account: snapshot.account, logged_in: null, checked_at: NOW, error: 'fake error' } }]);
    expect(slot(key)).toBe('Retry');
    expect(row(key)!.querySelector('.status-dot')?.getAttribute('data-shape')).toBe('ring');
    fireEvent.click(row(key)!.querySelector('.row-action')!);
    await act(async () => { await Promise.resolve(); });
    expect(api.checkAccount).toHaveBeenCalledTimes(1);
    checks([snapshot]);
    expect(slot(key)).toBe('Checking…');
    checks([{ ...snapshot, phase: 'complete', result: { account: snapshot.account, logged_in: false, checked_at: NOW, error: null } }]);
    expect(slot(key)).toBe('Log in…');
    expect(row(key)!.querySelector('.status-dot')?.getAttribute('data-shape')).toBe('ring');
    expect(screen.queryByRole('button', { name: /^Retry/ })).toBeNull();
    expect(screen.queryByRole('button', { name: /^Check for claude-unknown/ })).toBeNull();
  });

  it('retains shared results through native hide/reopen and never asks JS to start automatic checks', async () => {
    const { api, checks } = await renderPopover();
    checks([{ runtime: 'claude', account: 'claude-unknown', phase: 'complete',
      result: { account: 'claude-unknown', logged_in: false, checked_at: NOW, error: null } }]);
    hide();
    show();
    expect(slot('claude:claude-unknown')).toBe('Log in…');
    expect(api.checkAccount).not.toHaveBeenCalled();
    expect(api.getAccountChecks).toHaveBeenCalledTimes(1);
  });

  it('first open with several checking accounts has no moving loaders when usage is not reading', async () => {
    const first = envelope(null);
    first.usage = { ...usage, providers: [{ ...usage.providers[0]!, accounts: Array.from({ length: 5 }, (_, index) => ({
      ...usage.providers[0]!.accounts.find((account) => account.name === 'claude-unknown')!, name: `unknown-${index}`,
    })) }] };
    await renderPopover({ ...first, reading: false });
    show();
    expect(document.querySelectorAll('.checking-slot')).toHaveLength(5);
    expect(document.querySelectorAll('.is-turning')).toHaveLength(0);
    expect(document.querySelector('.row-slot .turning-icon')).toBeNull();
    for (let index = 0; index < 5; index += 1) {
      expect(slot(`claude:unknown-${index}`)).toBe('Checking…');
      expect(screen.getByRole('status', { name: `Checking unknown-${index}` })).toBeTruthy();
    }
  });

  it('Reduce Motion keeps the static bar and accessible Checking… status without a spinner', async () => {
    media.reduced = true;
    await renderPopover();
    expect(slot('claude:claude-unknown')).toBe('Checking…');
    const checking = within(row('claude:claude-unknown')!).getByRole('status', { name: 'Checking claude-unknown' });
    expect(checking.querySelector('.skeleton-bar')?.getAttribute('aria-hidden')).toBe('true');
    expect(checking.querySelector('.visually-hidden')?.textContent).toBe('Checking…');
    expect(row('claude:claude-unknown')!.querySelector('.turning-icon')).toBeNull();
  });
});
