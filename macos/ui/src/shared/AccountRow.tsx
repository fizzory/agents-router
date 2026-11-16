// One account: dot, name (middle ellipsis), gauge, then a slot with the figure
// and reset time, a state word, or an inline button. The row is one focus
// target that opens the account in Settings; the inline button is a second
// target reached with ←/→ (see useRovingList).
import { useEffect, useRef, useState, type AnimationEvent, type CSSProperties, type MouseEvent, type ReactNode } from 'react';
import { Button } from './Button';
import { cssTime } from './motion';
import { RollingFigure } from './RollingFigure';
import { Spinner } from './Spinner';
import { StatusDot } from './StatusDot';
import type { RowModel } from './types';
import { UsageGauge } from './UsageGauge';
import { VisuallyHidden } from './VisuallyHidden';

export const ROW_LINK = 'link';
export const ROW_ACTION = 'action';
const STAGGER_ROWS = 8;

export interface AccountRowProps {
  model: RowModel;
  index: number;
  /** This render carries fresh usage: bars slide and digits roll. */
  animate: boolean;
  entering?: boolean;
  /** The panel is visible; spinners and rolls stop while it is hidden. */
  active: boolean;
  reduced: boolean;
  /** The popover's row target opens the account in Settings; Settings rows have none. */
  onOpen?: (model: RowModel) => void;
  onAction: (model: RowModel) => Promise<unknown>;
  tabIndex: (part: string) => number;
  register: (part: string) => (element: HTMLElement | null) => void;
  onFocusPart: (part: string) => () => void;
  /** Settings adds its switch and ⋯ here. */
  trailing?: ReactNode;
  /** Settings: the reason line and inline messages under the row. */
  below?: ReactNode;
  className?: string;
  rowRef?: (element: HTMLLIElement | null) => void;
  onAnimationEnd?: (event: AnimationEvent<HTMLLIElement>) => void;
  onContextMenu?: (event: MouseEvent) => void;
}

export function AccountRow({ model, index, animate, entering = false, active, reduced, onOpen, onAction, tabIndex, register, onFocusPart, trailing, below,
  className, rowRef, onAnimationEnd, onContextMenu }: AccountRowProps) {
  const [busy, setBusy] = useState(false);
  const [spinning, setSpinning] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);

  const run = async () => {
    if (busy || !model.action) return;
    setBusy(true);
    // Check shows its spinner at once; Login only after --terminal-delay.
    if (model.action === 'check') setSpinning(true);
    else timer.current = setTimeout(() => setSpinning(true), cssTime('--terminal-delay'));
    try { await onAction(model); }
    finally {
      clearTimeout(timer.current);
      setBusy(false);
      setSpinning(false);
    }
  };

  const split = Math.ceil(model.name.length / 2);
  const style = { '--i': Math.min(index, STAGGER_ROWS - 1) } as CSSProperties;
  const classes = ['account-row', entering ? 'row-enter' : '', className ?? ''].filter(Boolean).join(' ');
  return (
    <li ref={rowRef} className={classes} role="listitem" data-key={model.key} data-tooltip={model.tooltip} style={style}
      tabIndex={onOpen ? undefined : -1} onAnimationEnd={onAnimationEnd} onContextMenu={onContextMenu}>
      {onOpen ? (
        <button type="button" className="row-link" aria-label={model.linkLabel} tabIndex={tabIndex(ROW_LINK)}
          ref={register(ROW_LINK)} onFocus={onFocusPart(ROW_LINK)} onClick={() => onOpen(model)} />
      ) : <VisuallyHidden>{model.tooltip}</VisuallyHidden>}
      <StatusDot state={model.availability} />
      <span className="account-name" aria-hidden="true">
        <span className="name-start">{model.name.slice(0, split)}</span>
        <span className="name-end">{model.name.slice(split)}</span>
      </span>
      <UsageGauge meters={model.meters} hidden={!model.hasWindows} fresh={animate} />
      <span className={model.stale ? 'row-slot is-stale' : 'row-slot'}>
        {model.action ? (
          <Button variant="inline" className="row-action" busy={busy} tabIndex={tabIndex(ROW_ACTION)} ref={register(ROW_ACTION)}
            onFocus={onFocusPart(ROW_ACTION)} onClick={run} aria-label={busy ? model.busyLabel : model.accessibleLabel}>
            {spinning && <Spinner enabled={active && !reduced} />}
            {model.button}
          </Button>
        ) : model.figure ? (
          <>
            <RollingFigure className={model.threshold ? 'figure is-warn' : 'figure'} value={model.figure} animate={animate && !reduced} active={active} />
            <span className="reset-time" aria-hidden="true">{model.reset}</span>
          </>
        ) : model.state ? <span className="state-word" aria-hidden="true">{model.state}</span> : null}
      </span>
      {trailing}
      {below}
    </li>
  );
}
