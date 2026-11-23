// One account: dot, name (middle ellipsis), gauge, then a slot with the figure
// and reset time, a state word, or an inline button. The row is one focus
// target that opens in-place detail; the inline button is a second
// target reached with ←/→. Logged-out popover rows add copy as the third part
// (see useRovingList).
import { useEffect, useId, useLayoutEffect, useRef, useState, type AnimationEvent, type CSSProperties, type MouseEvent, type ReactNode } from 'react';
import { Button } from './Button';
import { CopyButton } from './CopyButton';
import { cssTime } from './motion';
import { RollingFigure } from './RollingFigure';
import { Spinner } from './Spinner';
import { StatusDot } from './StatusDot';
import type { RowModel } from './types';
import { UsageGauge } from './UsageGauge';
import { VisuallyHidden } from './VisuallyHidden';

export const ROW_LINK = 'link';
export const ROW_ACTION = 'action';
export const ROW_COPY = 'copy';
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
  /** The popover's row target toggles detail; Settings rows have none. */
  onOpen?: (model: RowModel) => void;
  expanded?: boolean;
  onSettings?: (model: RowModel) => void;
  loginCommand?: { get: () => Promise<string>; copy: (text: string) => Promise<unknown>; failed?: string; rejected?: string };
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
  expanded = false, onSettings, loginCommand, className, rowRef, onAnimationEnd, onContextMenu }: AccountRowProps) {
  const [busy, setBusy] = useState(false);
  const [spinning, setSpinning] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const detailId = useId();
  const rejectionId = `${detailId}-login-failure`;
  const detail = useRef<HTMLDivElement>(null);
  const [detailVisible, setDetailVisible] = useState(expanded);
  useEffect(() => () => clearTimeout(timer.current), []);
  useLayoutEffect(() => {
    if (!onOpen) return;
    if (expanded) { setDetailVisible(true); return; }
    // Remove outgoing content from layout immediately: the native panel owns
    // its 160ms height change. It can still fade while clipped by that panel.
    if (detail.current?.contains(document.activeElement)) {
      detail.current.parentElement?.querySelector<HTMLButtonElement>('.row-link')?.focus();
    }
    const close = setTimeout(() => setDetailVisible(false), cssTime('--cross-fade-time'));
    return () => clearTimeout(close);
  }, [expanded, !!onOpen]);

  const run = async () => {
    if (busy || !model.action) return;
    setBusy(true);
    // Check shows its spinner at once; Login only after --terminal-delay.
    if (['check', 'retry'].includes(model.action)) setSpinning(true);
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
  const classes = ['account-row', onOpen ? 'popover-account' : '', entering ? 'row-enter' : '', className ?? ''].filter(Boolean).join(' ');
  const main = <>
      {onOpen ? (
        <button type="button" className="row-link" aria-label={model.linkLabel} tabIndex={tabIndex(ROW_LINK)}
          aria-expanded={expanded} aria-controls={detailId}
          ref={register(ROW_LINK)} onFocus={onFocusPart(ROW_LINK)} onClick={() => onOpen(model)} />
      ) : <VisuallyHidden>{model.tooltip}</VisuallyHidden>}
      <StatusDot state={model.availability} shape={onOpen ? model.shape : undefined} breathing={model.breathing} />
      <span className="account-name" aria-hidden="true">
        <span className="name-start">{model.name.slice(0, split)}</span>
        <span className="name-end">{model.name.slice(split)}</span>
      </span>
      <UsageGauge meters={model.meters} hidden={!model.hasWindows} fresh={animate && active && !reduced} />
      <span key={model.action || model.state || (model.stale ? 'stale-usage' : 'usage')} className={model.stale ? 'row-slot is-stale' : 'row-slot'}>
        {model.action === 'login' && loginCommand && <CopyButton variant="icon" className="row-copy"
          value={loginCommand.get} copy={loginCommand.copy} failureFeedback tooltip="Copy login command"
          accessibleName={`Copy login command for ${model.name}`} tabIndex={tabIndex(ROW_COPY)} ref={register(ROW_COPY)}
          onFocus={onFocusPart(ROW_COPY)} />}
        {model.checking ? <span className="checking-slot" role="status" aria-label={`Checking ${model.name}`}>
          <span className="skeleton-bar" aria-hidden="true" />
          <VisuallyHidden>Checking…</VisuallyHidden>
        </span> : model.action ? (
          <Button variant="inline" className="row-action" busy={busy} tabIndex={tabIndex(ROW_ACTION)} ref={register(ROW_ACTION)}
            aria-describedby={model.action === 'login' && loginCommand?.rejected ? rejectionId : undefined}
            onFocus={onFocusPart(ROW_ACTION)} onClick={run} aria-label={busy ? model.busyLabel : model.accessibleLabel}>
            {spinning && !reduced && <Spinner enabled={active} />}
            {model.button}
          </Button>
        ) : model.figure ? (
          <>
            {onOpen && <span className="window-tag" aria-hidden="true">{model.windowTag}</span>}
            <RollingFigure className={model.threshold ? 'figure is-warn' : 'figure'} value={model.figure} animate={animate && !reduced} active={active} />
            <span className="reset-time" aria-hidden="true">{model.reset}</span>
          </>
        ) : model.state ? <span className="state-word" aria-hidden="true">{model.state}</span> : null}
      </span>
      {trailing}
    </>;
  return (
    <li ref={rowRef} className={classes} role="listitem" data-key={model.key} data-tooltip={onOpen ? undefined : model.tooltip} style={style}
      tabIndex={onOpen ? undefined : -1} onAnimationEnd={onAnimationEnd} onContextMenu={onContextMenu}>
      {onOpen ? <div className={model.dimmed ? 'account-main is-dimmed' : 'account-main'}>{main}</div> : main}
      {onOpen && <div id={detailId} ref={detail} className="account-detail" data-open={expanded} hidden={!detailVisible}
        inert={!expanded} aria-hidden={!expanded} onKeyDown={(event) => {
          if (['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) event.stopPropagation();
        }}>
        {model.action === 'login' && loginCommand?.rejected && <div id={rejectionId} data-tooltip={loginCommand.rejected}>Couldn't start login.</div>}
        {model.action === 'login' && !loginCommand?.rejected && loginCommand?.failed && <div className="login-failure">
          <span>Couldn't open Terminal.</span>
          <CopyButton key={loginCommand.failed} variant="text" value={loginCommand.failed} copy={loginCommand.copy} failureFeedback />
        </div>}
        {model.detailLines.map((line) => <div key={line}>{line}</div>)}
        <div className="account-context"><span>{model.detailContext}</span>
          <Button variant="text" aria-label={`Settings for ${model.name}`} onClick={() => onSettings?.(model)}>Settings…</Button>
        </div>
      </div>}
      {below}
    </li>
  );
}
