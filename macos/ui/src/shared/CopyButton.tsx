// Copy a command, key or path through the native pasteboard. Reads "Copied"
// for --copied-delay, then returns; VoiceOver hears "Copied".
import { useEffect, useRef, useState } from 'react';
import { useAnnounce } from './announce';
import { Button } from './Button';
import { Icon } from './Icon';
import { cssTime } from './motion';

export interface CopyButtonProps {
  value: string;
  copy: (text: string) => Promise<unknown>;
  label?: string;
  variant?: 'push' | 'text' | 'icon';
  accessibleName?: string;
  tooltip?: string;
  onError?: (error: unknown) => void;
  disabled?: boolean;
}

export function CopyButton({ value, copy, label = 'Copy command', variant = 'push', accessibleName, tooltip, onError, disabled }: CopyButtonProps) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const announce = useAnnounce();
  useEffect(() => () => clearTimeout(timer.current), []);
  const click = async () => {
    try {
      await copy(value);
      setCopied(true);
      announce('Copied');
      clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), cssTime('--copied-delay'));
    } catch (error) { onError?.(error); }
  };
  const name = copied ? 'Copied' : accessibleName;
  return (
    <Button variant={variant} className="copy-button" aria-label={name} tooltip={tooltip ?? value} disabled={disabled} onClick={click}>
      {variant === 'icon' ? <Icon name={copied ? 'check' : 'copy'} /> : copied ? 'Copied' : label}
    </Button>
  );
}
