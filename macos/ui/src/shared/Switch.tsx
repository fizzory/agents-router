// Booleans. role=switch with aria-checked; an unset value (null) shows at half
// opacity. Disabled switches stay focusable (aria-disabled) and say why.
export interface SwitchProps {
  id?: string;
  checked: boolean | null;
  onChange: (next: boolean) => void;
  label?: string;
  describedBy?: string;
  disabled?: boolean;
  /** Shown as the tooltip of a disabled switch. */
  disabledReason?: string;
  /** Another write is running: ignore input without dimming. */
  locked?: boolean;
  busy?: boolean;
  small?: boolean;
  invalid?: boolean;
  errorId?: string;
  className?: string;
}

export function Switch({ id, checked, onChange, label, describedBy, disabled = false, disabledReason, locked = false, busy = false,
  small = false, invalid = false, errorId, className }: SwitchProps) {
  const blocked = disabled || !!disabledReason;
  const classes = ['switch', small ? 'is-small' : '', className ?? ''].filter(Boolean).join(' ');
  return (
    <button type="button" role="switch" id={id} className={classes} aria-checked={checked === true}
      data-unset={checked == null || undefined} aria-label={label} aria-describedby={describedBy}
      aria-disabled={blocked || undefined} aria-busy={busy || undefined} aria-invalid={invalid || undefined}
      aria-errormessage={invalid ? errorId : undefined} data-tooltip={disabledReason}
      onClick={() => { if (!blocked && !locked && !busy) onChange(checked !== true); }}>
      <span className="switch-thumb" />
    </button>
  );
}
