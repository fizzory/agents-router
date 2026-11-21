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
  const last = useRef({ meters, changed: new Set<string>() });
  if (meters !== last.current.meters) {
    const previous = new Map(last.current.meters.map(meter => [meter.name, meter]));
    const changed = new Set(meters.filter(meter => fresh && meter.present && previous.get(meter.name)?.present
      && previous.get(meter.name)?.percent !== meter.percent).map(meter => meter.name));
    last.current = { meters, changed };
  }
  const classes = ['usage-gauge', fresh && last.current.changed.size ? 'is-fresh' : '', hidden ? 'is-hidden' : ''].filter(Boolean).join(' ');
  return (
    <span className={classes} aria-hidden="true">
      {meters.map((meter) => {
        const bar = ['gauge-bar', meter.dominant ? 'is-main' : 'is-other', meter.warn || (meter.dominant && meter.percent >= WARN_PERCENT) ? 'is-warn' : '',
          fresh && last.current.changed.has(meter.name) ? 'is-changing' : '', meter.dimmed ? 'is-stale' : '', meter.reset ? 'is-reset' : ''].filter(Boolean).join(' ');
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
