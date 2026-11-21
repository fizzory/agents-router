// Copy a command, key or path through the native pasteboard. Reads "Copied"
// for --copied-delay, then returns; VoiceOver hears "Copied".
import { useEffect, useRef, useState } from 'react';
import { useAnnounce } from './announce';
import { Button, type ButtonProps } from './Button';
import { Icon } from './Icon';
import { cssTime } from './motion';

export interface CopyButtonProps extends Pick<ButtonProps, 'tabIndex' | 'ref' | 'onFocus' | 'onKeyDown' | 'className'> {
  value: string | (() => Promise<string>);
  copy: (text: string) => Promise<unknown>;
  label?: string;
  variant?: 'push' | 'text' | 'icon';
  accessibleName?: string;
  tooltip?: string;
  onError?: (error: unknown) => void;
  /** Popover feedback stays on the pressed control; Settings keeps onError. */
  failureFeedback?: boolean;
  disabled?: boolean;
}

export function CopyButton({ value, copy, label = 'Copy command', variant = 'push', accessibleName, tooltip, onError, failureFeedback = false, disabled, className, ...rest }: CopyButtonProps) {
  const [feedback, setFeedback] = useState<'idle' | 'copied' | 'failed'>('idle');
  const copied = feedback === 'copied', failed = feedback === 'failed';
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const announce = useAnnounce();
  const copying = useRef(false);
  useEffect(() => () => clearTimeout(timer.current), []);
  const click = async () => {
    if (copying.current) return;
    copying.current = true;
    try {
      await copy(typeof value === 'function' ? await value() : value);
      setFeedback('copied');
      announce('Copied');
      clearTimeout(timer.current);
      timer.current = setTimeout(() => setFeedback('idle'), cssTime('--copied-delay'));
    } catch (error) {
      if (failureFeedback) {
        setFeedback('failed');
        announce("Couldn't copy.");
        clearTimeout(timer.current);
        timer.current = setTimeout(() => setFeedback('idle'), cssTime('--copied-delay'));
      }
      onError?.(error);
    }
    finally { copying.current = false; }
  };
  const name = accessibleName ?? (copied ? 'Copied' : failed ? "Couldn't copy" : undefined);
  return (
    <Button variant={variant} className={['copy-button', className].filter(Boolean).join(' ')} aria-label={name}
      tooltip={failed ? "Couldn't copy." : tooltip ?? (typeof value === 'string' ? value : undefined)} disabled={disabled} onClick={click} {...rest}>
      {variant === 'icon' ? <span className="copy-feedback" data-copied={copied} data-failed={failed}>
        <Icon name="copy" className="copy-glyph" /><Icon name="check" className="copy-check" /><Icon name="alert" className="copy-alert" />
      </span> : copied ? 'Copied' : failed ? "Couldn't copy" : label}
    </Button>
  );
}
