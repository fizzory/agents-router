import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { envelope, NOW, renderPopover, row } from '../test/popover';
import { envelope as settingsEnvelope, NOW as SETTINGS_NOW, renderSettings } from '../test/settings';
import sharedStyles from './shared.css?raw';
import { UsageGauge } from './UsageGauge';
import type { Meter } from './types';

const usage = (now: number, name = 'claude-ntb', statusline_usage?: string) => ({
  schema_version: 1, generated_at: now,
  providers: [{ runtime: 'claude', label: 'Claude', accounts: [{
    name, enabled: true, availability: 'ok', until: null, reason: null, problem: null,
    running_jobs: 0, busy_slots: 0, capacity: null, statusline_usage,
    windows: ['five_hour', 'seven_day'].map((name, index) => ({ name, label: index ? 'week' : '5h',
      used_percent: 0, reset: true, stale: false, observed_at: now - 20 * 86400,
      resets_at: now - (index ? 13 : 19) * 86400 })),
  }] }],
});

describe('S3 unknown since reset', () => {
  it('shows a dim dash and two dashed empty tracks, and explains each unknown window in detail', async () => {
    render(<style>{sharedStyles}</style>);
    await renderPopover({ ...envelope(null), usage: usage(NOW) });
    const target = row('claude:claude-ntb')!;
    expect(target.querySelector('.figure')?.textContent).toBe('–');
    expect(target.querySelector('.reset-time')?.textContent).toBe('20d ago');
    expect(target.querySelector('.window-tag')?.textContent).toBe('');
    expect(getComputedStyle(target.querySelector('.figure')!).opacity).toBe('0.5');
    const bars = [...target.querySelectorAll('.gauge-bar')];
    expect(bars).toHaveLength(2);
    for (const bar of bars) {
      expect(getComputedStyle(bar).borderTopStyle).toBe('dashed');
      expect(getComputedStyle(bar).opacity).toBe('0.5');
      expect(bar.querySelector('.gauge-fill')).toBeNull();
    }
    const link = target.querySelector<HTMLButtonElement>('.row-link')!;
    expect(link.getAttribute('aria-label')).toBe('Claude claude-ntb. Available. 5h unknown since reset, last seen 20d ago. Week unknown since reset, last seen 20d ago. Show account detail.');
    fireEvent.click(link);
    expect(screen.getByText('5 hours – Reset, not seen since')).toBeTruthy();
    expect(screen.getByText('Week – Reset, not seen since')).toBeTruthy();
    expect(screen.getByText('Updated 20d ago')).toBeTruthy();
    expect(target.textContent).not.toContain('0%');
  });

  it('uses the same unknown slot in Settings and offers agt setup only for missing or replaced taps', async () => {
    const view = await renderSettings({ target: { pane: 'accounts' } });
    for (const state of ['missing', 'replaced', 'off', 'future-state', 'live']) {
      await view.emit('onSettingsStatus', { ...settingsEnvelope(2), usage: usage(SETTINGS_NOW / 1000, 'work', state) });
      const target = document.querySelector('.settings-account[data-key="claude:work"]')!;
      expect(target.querySelector('.figure')?.textContent).toBe('–');
      expect(target.querySelector('.reset-time')?.textContent).toBe('20d ago');
      expect(target.querySelector('.row-slot')?.classList.contains('is-stale')).toBe(true);
      expect(target.querySelectorAll('.gauge-bar.is-reset.is-stale')).toHaveLength(2);
      if (state === 'missing' || state === 'replaced') {
        expect(target.querySelector('code')?.textContent).toBe('agt setup');
        expect(target.querySelector('.account-reason')?.textContent).toContain("Usage updates only from agt's jobs · Updated 20d ago");
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
