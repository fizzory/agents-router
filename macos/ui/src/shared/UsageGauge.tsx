// The usage gauge: two 4px bars, 5-hour on top and 7-day below. The busier
// window is the main bar. Bars slide only for fresh usage (is-fresh); any other
// change of values snaps.
import { useRef, type CSSProperties } from 'react';
import type { Meter } from './types';

export const WARN_PERCENT = 95;

export interface UsageGaugeProps {
  meters: Meter[];
  /** Kiro and Antigravity have no gauge; the space stays reserved. */
  hidden?: boolean;
  fresh?: boolean;
}

export function UsageGauge({ meters, hidden = false, fresh = false }: UsageGaugeProps) {
  const last = useRef<{ values: string; slide: boolean }>({ values: '', slide: false });
  const values = meters.map((meter) => `${meter.percent}`).join('/');
  if (fresh) last.current = { values, slide: true };
  else if (values !== last.current.values) last.current = { values, slide: false };
  const classes = ['usage-gauge', last.current.slide ? 'is-fresh' : '', hidden ? 'is-hidden' : ''].filter(Boolean).join(' ');
  return (
    <span className={classes} aria-hidden="true">
      {meters.map((meter) => {
        const bar = ['gauge-bar', meter.dominant ? 'is-main' : 'is-other', meter.dominant && meter.percent >= WARN_PERCENT ? 'is-warn' : '',
          meter.dimmed ? 'is-stale' : '', meter.reset ? 'is-reset' : ''].filter(Boolean).join(' ');
        const fill: CSSProperties = { transform: `translateX(${meter.percent - 100}%)` };
        return (
          <span key={meter.name} className={bar} data-window={meter.name}>
            <span className="gauge-fill" style={fill} />
          </span>
        );
      })}
    </span>
  );
}
