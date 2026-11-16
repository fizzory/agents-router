// A macOS popup button: the value and ⌃⌄, opening a Menu fixed under it.
import { useId, useRef, useState, type ReactNode } from 'react';
import { Icon } from './Icon';
import { Menu, type MenuEntry } from './Menu';

export interface PopupButtonProps {
  id?: string;
  /** The menu's accessible name. */
  label: string;
  /** The button's accessible name when it differs from its text. */
  ariaLabel?: string;
  text: ReactNode;
  entries: MenuEntry[];
  describedBy?: string;
  inherited?: boolean;
  disabled?: boolean;
  disabledReason?: string;
  locked?: boolean;
  invalid?: boolean;
  errorId?: string;
  tooltip?: string;
  className?: string;
}

export function PopupButton({ id, label, ariaLabel, text, entries, describedBy, inherited, disabled = false, disabledReason, locked = false,
  invalid = false, errorId, tooltip, className }: PopupButtonProps) {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const menuId = useId();
  const blocked = disabled || !!disabledReason;
  const classes = ['popup-button', inherited ? 'is-inherited' : '', className ?? ''].filter(Boolean).join(' ');
  return (
    <>
      <button ref={trigger} type="button" id={id} className={classes} aria-label={ariaLabel} aria-haspopup="menu" aria-expanded={open}
        aria-controls={open ? menuId : undefined} aria-describedby={describedBy} aria-disabled={blocked || undefined}
        aria-invalid={invalid || undefined} aria-errormessage={invalid ? errorId : undefined} data-tooltip={disabledReason ?? tooltip}
        onClick={() => { if (!blocked && !locked) setOpen((value) => !value); }}>
        <span className="popup-text">{text}</span>
        <Icon name="chevrons-updown" />
      </button>
      {open && (
        <Menu id={menuId} label={label} open entries={entries} trigger={trigger} anchored
          onClose={(focusTrigger) => { setOpen(false); if (focusTrigger) trigger.current?.focus(); }} />
      )}
    </>
  );
}
