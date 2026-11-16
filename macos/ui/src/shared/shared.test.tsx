import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cssTime } from './motion';
import { RollingFigure } from './RollingFigure';
import { TooltipProvider } from './Tooltip';
import type { Meter } from './types';
import { UsageGauge } from './UsageGauge';

const meter = (name: string, percent: number, extra: Partial<Meter> = {}): Meter =>
  ({ name, present: true, percent, reset: false, dimmed: false, dominant: false, ...extra });
const bars = () => [...document.querySelectorAll<HTMLElement>('.gauge-bar')];

describe('Tooltip', () => {
  afterEach(() => vi.useRealTimers());

  it('adds only its own id to aria-describedby and removes only that id', () => {
    render(<TooltipProvider><span id="hint">Own description</span>
      <button type="button" aria-describedby="hint" data-tooltip={'First line\nSecond line'}>Target</button></TooltipProvider>);
    const target = screen.getByRole('button', { name: 'Target' });
    act(() => target.focus());
    expect(target.getAttribute('aria-describedby')).toBe('hint tooltip');
    expect(screen.getByRole('tooltip').textContent).toBe('First line\nSecond line');
    act(() => target.blur());
    expect(target.getAttribute('aria-describedby')).toBe('hint');
    expect(screen.getByRole('tooltip', { hidden: true }).hidden).toBe(true);
  });

  it('waits --tooltip-delay on first hover, then is warm', () => {
    vi.useFakeTimers();
    render(<TooltipProvider><button type="button" data-tooltip="One">A</button><button type="button" data-tooltip="Two">B</button></TooltipProvider>);
    fireEvent.pointerOver(screen.getByText('A'));
    expect(screen.getByRole('tooltip', { hidden: true }).hidden).toBe(true);
    act(() => { vi.advanceTimersByTime(cssTime('--tooltip-delay')); });
    expect(screen.getByRole('tooltip').textContent).toBe('One');
    fireEvent.pointerOver(screen.getByText('B'));
    expect(screen.getByRole('tooltip').textContent).toBe('Two');
    expect(screen.getByText('A').hasAttribute('aria-describedby')).toBe(false);
  });
});

describe('UsageGauge', () => {
  it('main and other bars, warn at 95%, stale at half opacity, reset empty, hidden keeps its space', () => {
    const { rerender } = render(<UsageGauge meters={[meter('five_hour', 30), meter('seven_day', 96, { dominant: true })]} />);
    const [five, seven] = bars();
    expect(five?.classList.contains('is-other')).toBe(true);
    expect(seven?.classList.contains('is-main')).toBe(true);
    expect(seven?.classList.contains('is-warn')).toBe(true);
    expect((seven?.firstElementChild as HTMLElement).style.transform).toBe('translateX(-4%)');
    rerender(<UsageGauge meters={[meter('five_hour', 0, { reset: true }), meter('seven_day', 40, { dominant: true, dimmed: true })]} />);
    expect(bars()[0]?.classList.contains('is-reset')).toBe(true);
    expect((bars()[0]?.firstElementChild as HTMLElement).style.transform).toBe('translateX(-100%)');
    expect(bars()[1]?.classList.contains('is-stale')).toBe(true);
    expect(bars()[1]?.classList.contains('is-warn')).toBe(false);
    rerender(<UsageGauge hidden meters={[meter('five_hour', 0), meter('seven_day', 0)]} />);
    const gauge = document.querySelector('.usage-gauge');
    expect(gauge?.classList.contains('is-hidden')).toBe(true);
    expect(bars()).toHaveLength(2);
    expect(gauge?.getAttribute('aria-hidden')).toBe('true');
  });

  it('slides only for fresh values; a later non-fresh change snaps', () => {
    const { rerender } = render(<UsageGauge fresh meters={[meter('five_hour', 10)]} />);
    expect(document.querySelector('.usage-gauge')?.classList.contains('is-fresh')).toBe(true);
    rerender(<UsageGauge meters={[meter('five_hour', 10)]} />);
    expect(document.querySelector('.usage-gauge')?.classList.contains('is-fresh')).toBe(true);
    rerender(<UsageGauge meters={[meter('five_hour', 20)]} />);
    expect(document.querySelector('.usage-gauge')?.classList.contains('is-fresh')).toBe(false);
  });
});

describe('RollingFigure', () => {
  it('rolls only changed digits, cross-fades bigger changes, snaps without animation', () => {
    const { rerender } = render(<RollingFigure value="42%" animate />);
    rerender(<RollingFigure value="43%" animate />);
    expect(document.querySelectorAll('.figure-strip')).toHaveLength(1);
    fireEvent.animationEnd(document.querySelector('.figure-strip')!);
    expect(document.querySelector('.rolling-figure')?.textContent).toBe('43%');
    rerender(<RollingFigure value="100%" animate />);
    expect(document.querySelector('.figure-before')?.textContent).toBe('43%');
    expect(document.querySelector('.figure-after')?.textContent).toBe('100%');
    rerender(<RollingFigure value="7%" animate={false} />);
    expect(document.querySelector('.figure-strip, .figure-before')).toBeNull();
    expect(document.querySelector('.rolling-figure')?.textContent).toBe('7%');
  });
});
