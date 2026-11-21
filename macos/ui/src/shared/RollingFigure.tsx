// Percentage digits. On fresh usage only the changed digits roll; more than two
// changes cross-fade; reduced motion and a hidden panel snap.
import { useLayoutEffect, useRef, useState } from 'react';

export interface RollingFigureProps {
  value: string;
  animate: boolean;
  /** False while the panel is hidden: any roll settles at once. */
  active?: boolean;
  className?: string;
}

const WIDTH = 4;

export function RollingFigure({ value, animate, active = true, className }: RollingFigureProps) {
  const previous = useRef(value);
  const [roll, setRoll] = useState<{ from: string; to: string } | null>(null);
  useLayoutEffect(() => {
    if (value === previous.current) return;
    setRoll(animate && previous.current && value ? { from: previous.current, to: value } : null);
    previous.current = value;
  }, [value, animate]);
  useLayoutEffect(() => {
    if (!active || !animate) setRoll(null);
  }, [active, animate]);

  const classes = className ? `rolling-figure ${className}` : 'rolling-figure';
  if (!roll || roll.to !== value) return <span className={classes} aria-hidden="true">{value}</span>;
  const before = roll.from.padStart(WIDTH, ' ');
  const after = roll.to.padStart(WIDTH, ' ');
  const changed = [...after].filter((char, index) => char !== before[index]).length;
  const settle = () => setRoll(null);
  if (changed > 2) {
    return (
      <span className={`${classes} is-rolling`} aria-hidden="true" onAnimationEnd={settle}>
        <span className="figure-before">{roll.from}</span>
        <span className="figure-after">{roll.to}</span>
      </span>
    );
  }
  return (
    <span className={`${classes} is-rolling`} aria-hidden="true" onAnimationEnd={settle}>
      {[...after].map((char, index) => char === before[index] ? char : (
        <span key={index} className="figure-digit">
          <span className="figure-strip"><span>{before[index]}</span><span>{char}</span></span>
        </span>
      ))}
    </span>
  );
}
