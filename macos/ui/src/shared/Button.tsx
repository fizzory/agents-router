// Every push button: push (bordered), default (accent fill), text, icon and inline.
import type { ButtonHTMLAttributes, MouseEvent, Ref } from 'react';

export type ButtonVariant = 'push' | 'default' | 'text' | 'icon' | 'inline';

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  /** Busy keeps the button's width, sets aria-busy and ignores clicks. */
  busy?: boolean;
  /** A disabled button that says why: it stays focusable and shows the reason as its tooltip. */
  disabledReason?: string;
  tooltip?: string;
  ref?: Ref<HTMLButtonElement>;
}

export function Button({ variant = 'push', busy = false, disabledReason, tooltip, className, onClick, type = 'button', ...rest }: ButtonProps) {
  const blocked = busy || !!disabledReason;
  const classes = ['button', `button-${variant}`, busy ? 'is-busy' : '', className ?? ''].filter(Boolean).join(' ');
  const click = (event: MouseEvent<HTMLButtonElement>) => {
    if (blocked) { event.preventDefault(); return; }
    onClick?.(event);
  };
  return (
    <button type={type} className={classes} onClick={click}
      aria-busy={busy || undefined} aria-disabled={blocked || undefined}
      data-tooltip={disabledReason ?? tooltip} {...rest} />
  );
}
