// Turning icons finish their current turn before stopping, so nothing jumps.
import { useCallback, useEffect, useRef, useState } from 'react';
import { Icon, type IconName } from './Icon';

export function useTurning(busy: boolean, enabled: boolean) {
  const [turning, setTurning] = useState(false);
  const busyRef = useRef(busy);
  busyRef.current = busy;
  useEffect(() => {
    if (!enabled) setTurning(false);
    else if (busy) setTurning(true);
  }, [busy, enabled]);
  const onAnimationIteration = useCallback(() => {
    if (!busyRef.current) setTurning(false);
  }, []);
  return { turning, onAnimationIteration };
}

export interface TurningIconProps {
  name?: IconName;
  busy: boolean;
  /** False while the panel is hidden or motion is reduced: the icon stands still. */
  enabled: boolean;
}

export function TurningIcon({ name = 'refresh', busy, enabled }: TurningIconProps) {
  const { turning, onAnimationIteration } = useTurning(busy, enabled);
  const classes = ['turning-icon', turning ? 'is-turning' : '', busy ? 'is-busy' : ''].filter(Boolean).join(' ');
  return <Icon name={name} className={classes} onAnimationIteration={onAnimationIteration} />;
}

export function Spinner({ enabled }: { enabled: boolean }) {
  return <TurningIcon name="spinner" busy enabled={enabled} />;
}
