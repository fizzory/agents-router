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
    expect(bars()[0]?.querySelector('.gauge-fill')).toBeNull();
    expect(bars()[1]?.classList.contains('is-stale')).toBe(true);
    expect(bars()[1]?.classList.contains('is-warn')).toBe(false);
    rerender(<UsageGauge hidden meters={[meter('five_hour', 0), meter('seven_day', 0)]} />);
    const gauge = document.querySelector('.usage-gauge');
    expect(gauge?.classList.contains('is-hidden')).toBe(true);
    expect(bars()).toHaveLength(2);
    expect(gauge?.getAttribute('aria-hidden')).toBe('true');
  });

  it('springs only a changed bar on fresh usage; initial, unchanged and status values stay still', () => {
    const values = (five: number, seven: number) => [meter('five_hour', five), meter('seven_day', seven)];
    const changing = () => bars().filter(bar => bar.classList.contains('is-changing')).map(bar => bar.dataset.window);
    const { rerender } = render(<UsageGauge fresh meters={values(10, 42)} />);
    expect(changing()).toEqual([]);
    rerender(<UsageGauge fresh meters={values(10, 44)} />);
    expect(changing()).toEqual(['seven_day']);
    rerender(<UsageGauge fresh meters={values(10, 44)} />);
    expect(changing()).toEqual([]);
    rerender(<UsageGauge meters={values(20, 50)} />);
    expect(changing()).toEqual([]);
  });
});

describe('RollingFigure', () => {
  it('unchanged digits stay still on refresh and a later roll settles when motion is disabled', () => {
    const { rerender } = render(<RollingFigure value="42%" animate />);
    rerender(<RollingFigure value="42%" animate />);
    expect(document.querySelector('.is-rolling')).toBeNull();
    rerender(<RollingFigure value="44%" animate />);
    expect(document.querySelectorAll('.figure-strip')).toHaveLength(1);
    expect(document.querySelector('.figure-strip')?.textContent).toBe('24');
    rerender(<RollingFigure value="44%" animate={false} />);
    expect(document.querySelector('.is-rolling')).toBeNull();
    expect(document.querySelector('.rolling-figure')?.textContent).toBe('44%');
  });
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
