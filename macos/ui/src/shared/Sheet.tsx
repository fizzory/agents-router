// One SheetHost shows one sheet at a time; callers list candidates in priority
// order (installer, updater, Add account) and the first present one shows.
// A sheet is an alertdialog: focus is trapped, Return runs the default,
// Escape the mapped cancel, and focus returns to the control that opened it.
import { useEffect, useLayoutEffect, useRef, type KeyboardEvent, type ReactNode } from 'react';
import { Button } from './Button';

export interface SheetButton { label: string; action: string; disabled?: boolean }

export interface SheetProps {
  name: string;
  title: ReactNode;
  detail?: ReactNode;
  children?: ReactNode;
  buttons: SheetButton[];
  defaultAction?: string | null;
  /** Escape runs this; null means Escape does nothing. */
  cancelAction?: string | null;
  onAction: (action: string) => void;
  /** Buttons are locked while an action is waiting. */
  pending?: boolean;
  busy?: boolean;
  message?: { tone: 'error' | 'status'; text: string } | null;
  /** Return runs the default only when this allows it (for example a valid form). */
  canSubmit?: boolean;
  /** Focus this element first; otherwise the default button, then the sheet. */
  initialFocus?: () => HTMLElement | null;
}

const FOCUSABLE = 'button:not([disabled]):not([aria-disabled="true"]), input:not([disabled]), [tabindex="0"]';

export function Sheet({ name, title, detail, children, buttons, defaultAction = null, cancelAction = null, onAction, pending = false,
  busy = false, message, canSubmit = true, initialFocus }: SheetProps) {
  const node = useRef<HTMLFormElement>(null);
  const titleId = `${name}-title`;
  const detailId = `${name}-detail`;
  const messageId = `${name}-message`;
  useEffect(() => {
    const sheet = node.current;
    if (!sheet || sheet.contains(document.activeElement)) return;
    const first = initialFocus?.() ?? sheet.querySelector<HTMLElement>(`[data-action="${defaultAction ?? ''}"]`) ?? sheet;
    first.focus();
  });
  const keyDown = (event: KeyboardEvent<HTMLFormElement>) => {
    // An open menu inside the sheet (the Provider popup) owns Enter and Escape:
    // choosing an item must not also press the sheet's default or cancel button.
    const target = event.target as Element | null;
    if ((event.key === 'Enter' || event.key === 'Escape') && target?.closest?.('[role="menu"], [aria-expanded="true"]')) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      if (cancelAction && !pending) onAction(cancelAction);
    } else if (event.key === 'Enter' && !event.metaKey) {
      event.preventDefault();
      if (defaultAction && !pending && !busy && canSubmit) onAction(defaultAction);
    } else if (event.key === 'Tab') {
      const sheet = node.current;
      if (!sheet) return;
      const items = [...sheet.querySelectorAll<HTMLElement>(FOCUSABLE)];
      if (!items.length) { event.preventDefault(); return; }
      const index = items.indexOf(document.activeElement as HTMLElement);
      const next = event.shiftKey ? (index <= 0 ? items.length - 1 : index - 1) : (index + 1) % items.length;
      event.preventDefault();
      items[next]?.focus();
    }
  };
  return (
    <form ref={node} className={`sheet sheet-${name}`} role="alertdialog" aria-modal="true" aria-labelledby={titleId}
      aria-describedby={detail ? detailId : undefined} aria-busy={busy || pending || undefined} tabIndex={-1}
      onSubmit={(event) => event.preventDefault()} onKeyDown={keyDown}>
      <h2 id={titleId} className="sheet-title">{title}</h2>
      {detail ? <p id={detailId} className="sheet-detail">{detail}</p> : null}
      {children}
      {message ? <p id={messageId} className={`sheet-message is-${message.tone}`} role={message.tone === 'error' ? 'alert' : 'status'}>{message.text}</p> : null}
      {buttons.length ? (
        <div className="sheet-buttons">
          {buttons.map((button) => (
            <Button key={button.action} variant={button.action === defaultAction ? 'default' : 'push'} data-action={button.action}
              disabled={pending || button.disabled} onClick={() => onAction(button.action)}>{button.label}</Button>
          ))}
        </div>
      ) : null}
    </form>
  );
}

export interface SheetEntry { id: string; node: ReactNode }

export function SheetHost({ sheets, fallbackFocus }: { sheets: (SheetEntry | null | false)[]; fallbackFocus: () => HTMLElement | null }) {
  const current = sheets.find((sheet): sheet is SheetEntry => !!sheet) ?? null;
  const opener = useRef<Element | null>(null);
  // Layout effects run before the sheet's own focus effect, so activeElement is still the opener.
  useLayoutEffect(() => {
    if (current && !opener.current) opener.current = document.activeElement;
    if (!current && opener.current) {
      const previous = opener.current;
      opener.current = null;
      const target = previous instanceof HTMLElement && previous.isConnected && previous !== document.body ? previous : fallbackFocus();
      target?.focus();
    }
  }, [current, fallbackFocus]);
  if (!current) return null;
  return <div className="sheet-backdrop" key={current.id}>{current.node}</div>;
}

export interface ProgressBarProps { label: string; value?: number | null; max?: number | null; valueText?: string }

export function ProgressBar({ label, value, max, valueText }: ProgressBarProps) {
  const determinate = value != null && max != null && max > 0;
  return (
    <progress className="progress-bar" aria-label={label} aria-valuetext={valueText}
      value={determinate ? value : undefined} max={determinate ? max : undefined} />
  );
}
