// Motion values come only from motion.css tokens; code reads them here.
import { useSyncExternalStore } from 'react';

/** A motion.css time token in milliseconds ("--terminal-delay" -> 300). */
export function cssTime(name: string, element: Element = document.documentElement): number {
  const raw = getComputedStyle(element).getPropertyValue(name).trim();
  const value = parseFloat(raw);
  if (!Number.isFinite(value)) return 0;
  return raw.endsWith('ms') ? value : raw.endsWith('s') ? value * 1000 : value;
}

export const REDUCED_MOTION = '(prefers-reduced-motion: reduce)';

export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (changed) => {
      const list = matchMedia(query);
      list.addEventListener('change', changed);
      return () => list.removeEventListener('change', changed);
    },
    () => matchMedia(query).matches,
  );
}

export const useReducedMotion = () => useMediaQuery(REDUCED_MOTION);
