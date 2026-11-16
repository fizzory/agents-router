// A list that is one Tab stop. ↑/↓ and Home/End move between items (no wrap);
// ←/→ move between the parts of one item (for example a row target and its
// inline button). The focused item is remembered by key, so data refreshes keep it.
import { useCallback, useRef, useState, type KeyboardEvent } from 'react';

export interface RovingItem {
  key: string;
  /** Focusable parts, left to right. */
  parts: string[];
  /** The part that gets focus first when the item is entered. */
  initial: string;
}

const id = (key: string, part: string) => `${key}\u0000${part}`;

export function useRovingList(items: RovingItem[]) {
  const [active, setActive] = useState<{ key: string; part: string } | null>(null);
  const nodes = useRef(new Map<string, HTMLElement>());
  const item = items.find((entry) => entry.key === active?.key);
  const current = item && active && item.parts.includes(active.part) ? active
    : item ? { key: item.key, part: item.initial } : items[0] ? { key: items[0].key, part: items[0].initial } : null;

  const register = useCallback((key: string, part: string) => (element: HTMLElement | null) => {
    if (element) nodes.current.set(id(key, part), element);
    else nodes.current.delete(id(key, part));
  }, []);

  const focus = (key: string, part: string) => {
    setActive({ key, part });
    nodes.current.get(id(key, part))?.focus();
  };

  const onKeyDown = (event: KeyboardEvent) => {
    if (!current || event.altKey || event.metaKey || event.ctrlKey) return;
    const index = items.findIndex((entry) => entry.key === current.key);
    const entry = items[index];
    if (!entry) return;
    let target: RovingItem | undefined;
    if (event.key === 'ArrowDown') target = items[Math.min(items.length - 1, index + 1)];
    else if (event.key === 'ArrowUp') target = items[Math.max(0, index - 1)];
    else if (event.key === 'Home') target = items[0];
    else if (event.key === 'End') target = items[items.length - 1];
    else if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      const part = entry.parts.indexOf(current.part) + (event.key === 'ArrowLeft' ? -1 : 1);
      event.preventDefault();
      const next = entry.parts[Math.max(0, Math.min(entry.parts.length - 1, part))];
      if (next) focus(entry.key, next);
      return;
    } else return;
    event.preventDefault();
    if (target) focus(target.key, target === entry ? current.part : target.initial);
  };

  return {
    tabIndex: (key: string, part: string) => (current?.key === key && current.part === part ? 0 : -1),
    register,
    onKeyDown,
    onFocus: (key: string, part: string) => () => setActive({ key, part }),
  };
}
