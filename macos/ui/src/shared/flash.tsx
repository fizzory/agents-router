// Row highlight after navigation or undo: the target row scrolls into view,
// takes focus and flashes for --flash-time. A flash is used once.
import { createContext, useCallback, useContext, useLayoutEffect, useRef, useState } from 'react';
import { REDUCED_MOTION } from './motion';

export interface FlashTarget { key: string; serial: number }
interface FlashApi { target: FlashTarget | null; consume(serial: number): void }
export const FlashContext = createContext<FlashApi>({ target: null, consume() {} });

export function useFlash<T extends HTMLElement = HTMLDivElement>(key: string) {
  const { target, consume } = useContext(FlashContext);
  const node = useRef<T | null>(null);
  const [on, setOn] = useState(false);
  useLayoutEffect(() => {
    const element = node.current;
    if (!target || target.key !== key || !element) return;
    consume(target.serial);
    const details = element.closest('details');
    if (details && !details.open) details.open = true;
    element.scrollIntoView?.({ block: 'nearest', behavior: matchMedia(REDUCED_MOTION).matches ? 'auto' : 'smooth' });
    element.focus({ preventScroll: true });
    setOn(false);
    requestAnimationFrame(() => setOn(true));
  }, [target, key, consume]);
  const ref = useCallback((element: T | null) => { node.current = element; }, []);
  // Animations inside the row (a rolling figure) bubble here; only the row's own flash ends it.
  const onAnimationEnd = useCallback((event?: { target: EventTarget; currentTarget: EventTarget }) => {
    if (!event || event.target === event.currentTarget) setOn(false);
  }, []);
  return { ref, on, onAnimationEnd };
}
