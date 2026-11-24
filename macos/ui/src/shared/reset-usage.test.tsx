import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { envelope, NOW, renderPopover, row } from '../test/popover';
import { envelope as settingsEnvelope, fakeSettingsApi, NOW as SETTINGS_NOW, renderSettings, settle } from '../test/settings';
import { SettingsApp } from '../settings/SettingsApp';
import sharedStyles from './shared.css?raw';
import settingsStyles from '../settings/settings.css?raw';
import { UsageGauge } from './UsageGauge';
import type { Meter } from './types';

const usage = (now: number, name = 'claude-ntb', statusline_usage?: string) => ({
  schema_version: 1, generated_at: now,
  providers: [{ runtime: 'claude', label: 'Claude', accounts: [{
    name, enabled: true, availability: 'ok', until: null, reason: null, problem: null,
    running_jobs: 0, busy_slots: 0, capacity: null, statusline_usage,
    windows: ['five_hour', 'seven_day'].map((name, index) => ({ name, label: index ? 'week' : '5h',
      used_percent: 0, reset: true, stale: false, observed_at: now - 20 * 86400,
      resets_at: now - (index ? 13 : 19) * 86400, source: 'terminal' as string | null | undefined })),
  }] }],
});

describe('S3 unknown since reset', () => {
  it('keeps reset selectors free of track background and border overrides', () => {
    // jsdom cannot resolve the track's custom property, so compare declarations too.
    const rules = sharedStyles.replace(/\/\*[\s\S]*?\*\//g, '').split('}');
    for (const rule of rules) {
      const declarationsStart = rule.lastIndexOf('{');
      const selector = rule.slice(0, declarationsStart);
      if (selector.includes('.is-reset')) {
        expect(rule.slice(declarationsStart + 1), selector.trim())
          .not.toMatch(/(^|[;{\s])(background|border)[\w-]*\s*:/i);
      }
    }
  });

  it('shows a dim dash and two plain empty tracks, and explains each unknown window in detail', async () => {
    render(<style>{sharedStyles}</style>);
    const data = usage(NOW);
    const zero = usage(NOW, 'claude-zero').providers[0]!.accounts[0]!;
    zero.windows = zero.windows.map(window => ({ ...window, used_percent: 0, reset: false,
      observed_at: NOW - 60, resets_at: NOW + 3600 }));
    data.providers[0]!.accounts.push(zero);
    await renderPopover({ ...envelope(null), usage: data });
    const target = row('claude:claude-ntb')!;
    expect(target.querySelector('.figure')?.textContent).toBe('–');
    expect(target.querySelector('.reset-time')?.textContent).toBe('20d ago');
    expect(target.querySelector('.window-tag')?.textContent).toBe('');
    expect(getComputedStyle(target.querySelector('.figure')!).opacity).toBe('0.5');
    const zeroRow = row('claude:claude-zero')!;
    expect(zeroRow.querySelector('.figure')?.textContent).toBe('0%');
    const zeroBars = [...zeroRow.querySelectorAll('.gauge-bar:not(.is-reset)')];
    expect(zeroBars).toHaveLength(2);
    const plainStyle = getComputedStyle(zeroBars[0]!);
    for (const bar of zeroBars) expect(getComputedStyle(bar).opacity).toBe('1');
    const bars = [...target.querySelectorAll('.gauge-bar')];
    expect(bars).toHaveLength(2);
    for (const bar of bars) {
      expect(getComputedStyle(bar).borderTopStyle).toBe('none');
      expect(getComputedStyle(bar).backgroundColor).toBe(plainStyle.backgroundColor);
      expect(getComputedStyle(bar).backgroundImage).toBe(plainStyle.backgroundImage);
      expect(getComputedStyle(bar).opacity).toBe('0.5');
      expect(bar.querySelector('.gauge-fill')).toBeNull();
    }
    const link = target.querySelector<HTMLButtonElement>('.row-link')!;
    expect(link.getAttribute('aria-label')).toBe('Claude claude-ntb. Available. 5h unknown since reset, last seen 20d ago. Week unknown since reset, last seen 20d ago. Show account detail.');
    fireEvent.click(link);
    expect(screen.getByText('5 hours – Unknown since reset · Last seen 20d ago')).toBeTruthy();
    expect(screen.getByText('Week – Unknown since reset · Last seen 20d ago')).toBeTruthy();
    expect(screen.getByText('Last terminal Claude Code usage seen 20d ago')).toBeTruthy();
    expect(target.textContent).not.toContain('0%');
  });

  it('uses the same unknown slot in Settings and offers agt setup only for missing or replaced taps', async () => {
    render(<style>{sharedStyles}</style>);
    const view = await renderSettings({ target: { pane: 'accounts' } });
    for (const state of ['missing', 'replaced', 'off', 'future-state', 'live']) {
      await view.emit('onSettingsStatus', { ...settingsEnvelope(2), usage: usage(SETTINGS_NOW / 1000, 'work', state) });
      const target = document.querySelector('.settings-account[data-key="claude:work"]')!;
      expect(target.querySelector('.figure')?.textContent).toBe('–');
      expect(target.querySelector('.reset-time')?.textContent).toBe('20d ago');
      expect(target.querySelector('.row-slot')?.classList.contains('is-stale')).toBe(true);
      const bars = [...target.querySelectorAll('.gauge-bar.is-reset.is-stale')];
      expect(bars).toHaveLength(2);
      for (const bar of bars) {
        expect(getComputedStyle(bar).borderTopStyle).toBe('none');
        expect(getComputedStyle(bar).opacity).toBe('0.5');
        expect(bar.querySelector('.gauge-fill')).toBeNull();
      }
      if (state === 'missing' || state === 'replaced') {
        expect(target.querySelector('code')?.textContent).toBe('agt setup');
        expect(target.querySelector('.account-usage-context')?.textContent).toBe('Last terminal Claude Code usage seen 20d ago · Terminal updates need repair · agt setup');
      } else expect(target.querySelector('code')).toBeNull();
      expect(target.querySelector('.row-slot')?.textContent).not.toContain('0%');
    }
  });

  it('snaps into and out of an unknown gauge without a false zero animation or warning', () => {
    const known: Meter = { name: 'five_hour', present: true, percent: 98,
      reset: false, dimmed: false, dominant: true };
    const { rerender } = render(<UsageGauge fresh meters={[known]} />);
    expect(document.querySelector('.gauge-fill')).toBeTruthy();
    rerender(<UsageGauge fresh meters={[{ ...known, percent: 0, reset: true, dimmed: true, warn: true }]} />);
    expect(document.querySelector('.gauge-fill')).toBeNull();
    expect(document.querySelector('.is-changing, .is-warn')).toBeNull();
    rerender(<UsageGauge fresh meters={[{ ...known, percent: 12 }]} />);
    expect(document.querySelector('.gauge-fill')).toBeTruthy();
    expect(document.querySelector('.is-changing')).toBeNull();
  });
});

describe('Settings Claude source and age', () => {
  it('keeps the reset slot and always shows one wrapping source line with the displayed window age and repair command', async () => {
    render(<style>{settingsStyles}</style>);
    const view = await renderSettings({ target: { pane: 'accounts' } });
    const now = SETTINGS_NOW / 1000;
    const clock = (seconds: number) => new Date(seconds * 1000).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', hour12: false });
    let generation = 2;
    for (const source of ['terminal', 'job', null, undefined, 'future-source']) {
      for (const [state, suffix] of [
        ['live', ''], ['missing', ' · Terminal updates need repair'], ['replaced', ' · Terminal updates need repair'],
        ['off', ' · Terminal updates off'], ['unset', ' · Terminal updates not set up'],
        ['unsupported', ' · Terminal updates unavailable on Windows'], ['no_limits', ' · No usage limits on this login'],
      ]) {
        const data = usage(now, 'work', state);
        const account = data.providers[0]!.accounts[0]!;
        account.windows = [
          { ...account.windows[0]!, used_percent: 34, reset: false, observed_at: now - 60, resets_at: now + 3600, source: 'terminal' },
          { ...account.windows[1]!, used_percent: 65, reset: false, observed_at: now - 7200, resets_at: now + 86400, source },
        ];
        await view.emit('onSettingsStatus', { ...settingsEnvelope(generation++), usage: data });
        const target = document.querySelector('.settings-account[data-key="claude:work"]')!;
        const line = target.querySelector('.account-usage-context')!;
        const context = `${source === 'terminal' ? 'As of your last terminal Claude Code reply' : 'Last recorded usage'}, 2h ago${suffix}`;
        const repair = state === 'missing' || state === 'replaced' ? ' · agt setup' : '';
        expect(target.querySelectorAll('.account-usage-context')).toHaveLength(1);
        expect(line.textContent).toBe(context + repair);
        expect(line.closest('.account-detail')).toBeNull();
        expect((line as HTMLElement).hidden).toBe(false);
        expect(getComputedStyle(line).whiteSpace).toBe('normal');
        expect(getComputedStyle(line).textOverflow).toBe('clip');
        expect(getComputedStyle(line).overflow).toBe('visible');
        expect(target.querySelector('.figure')?.textContent).toBe('65%');
        expect(target.querySelector('.reset-time')?.textContent).toBe('Sat');
        expect(target.querySelector('.usage-figure-line')).toBeNull();
        expect(target.querySelector('.visually-hidden')?.textContent).toBe(`Claude work. Available. 5h 34%, as of your last terminal Claude Code reply, 1m ago, resets ${clock(now + 3600)}. Week 65%, stale, ${source === 'terminal' ? 'as of your last terminal Claude Code reply' : 'last recorded usage'}, 2h ago, resets Sat ${clock(now + 86400)}`);
      }
    }
  });

  it('shows no source line before a snapshot arrives, while its first read is pending, or after a failed read', async () => {
    const view = fakeSettingsApi({ target: { pane: 'accounts' } });
    view.api.getSnapshot.mockReturnValue(new Promise(() => {}));
    render(<SettingsApp api={view.api} now={() => SETTINGS_NOW} timeZone="UTC" />);
    await settle();
    const expectUnavailable = () => {
      const rows = [...document.querySelectorAll('.settings-account[data-key^="claude:"]')];
      expect(rows.length).toBeGreaterThan(0);
      for (const target of rows) {
        expect(target.querySelector('.account-usage-context')).toBeNull();
        expect(target.querySelector('.figure')).toBeNull();
        expect(target.textContent).not.toContain('No terminal usage received yet');
      }
    };
    expectUnavailable();
    await view.emit('onSettingsStatus', { ...settingsEnvelope(2), usage: null, reading: true });
    expectUnavailable();
    await view.emit('onSettingsStatus', { ...settingsEnvelope(3), usage: null, error: "Can't read agt", reading: false });
    expectUnavailable();
  });

  it('shows a read-but-empty context for every terminal-update state without a figure', async () => {
    const view = await renderSettings({ target: { pane: 'accounts' } });
    for (const [index, [state, suffix]] of [
      [undefined, ''], ['live', ''], ['future-state', ''],
      ['missing', ' · Terminal updates need repair · agt setup'], ['replaced', ' · Terminal updates need repair · agt setup'],
      ['off', ' · Terminal updates off'], ['unset', ' · Terminal updates not set up'],
      ['unsupported', ' · Terminal updates unavailable on Windows'], ['no_limits', ''],
    ].entries()) {
      const data = usage(SETTINGS_NOW / 1000, 'work', state);
      data.providers[0]!.accounts[0]!.windows = [];
      await view.emit('onSettingsStatus', { ...settingsEnvelope(index + 2), usage: data });
      const target = document.querySelector('.settings-account[data-key="claude:work"]')!;
      expect(target.querySelector('.figure')).toBeNull();
      expect(target.querySelector('.account-usage-context')?.textContent).toBe(state === 'no_limits'
        ? 'No usage limits on this login' : `No terminal usage received yet${suffix}`);
    }
  });
});

describe('Popover Claude no-limits context', () => {
  for (const source of ['terminal', 'job']) {
    it(`keeps the displayed ${source} figure source and age`, async () => {
      const data = usage(NOW, 'claude-ntb', 'no_limits');
      const account = data.providers[0]!.accounts[0]!;
      account.windows = [
        { ...account.windows[0]!, used_percent: 34, reset: false, observed_at: NOW - 60, resets_at: NOW + 3600, source: 'terminal' },
        { ...account.windows[1]!, used_percent: 65, reset: false, observed_at: NOW - 7200, resets_at: NOW + 86400, source },
      ];
      await renderPopover({ ...envelope(null), usage: data });
      const target = row('claude:claude-ntb')!;
      expect(target.querySelector('.figure')?.textContent).toBe('65%');
      expect(target.querySelector('.observation-age')?.textContent).toBe('2h ago');
      fireEvent.click(target.querySelector<HTMLButtonElement>('.row-link')!);
      expect(target.querySelector('.account-context > span')?.textContent).toBe(`${source === 'terminal' ? 'As of your last terminal Claude Code reply' : 'Last recorded usage'}, 2h ago · No usage limits on this login`);
    });
  }

  it('shows the no-limits context alone without a figure', async () => {
    const data = usage(NOW, 'claude-ntb', 'no_limits');
    data.providers[0]!.accounts[0]!.windows = [];
    await renderPopover({ ...envelope(null), usage: data });
    const target = row('claude:claude-ntb')!;
    expect(target.querySelector('.figure')).toBeNull();
    fireEvent.click(target.querySelector<HTMLButtonElement>('.row-link')!);
    expect(target.querySelector('.account-context > span')?.textContent).toBe('No usage limits on this login');
  });
});
