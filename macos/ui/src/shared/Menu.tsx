// A web menu with roving focus: ↑/↓ wrap, Home/End, type-ahead, Escape returns
// focus to the trigger, Tab or a click outside closes it. Only one is open at a time.
import { useEffect, useLayoutEffect, useRef, type KeyboardEvent, type RefObject } from 'react';

export type MenuEntry =
  | { kind: 'item'; id?: string; label: string; shortcut?: string; shortcutLabel?: string; disabled?: boolean; onSelect: () => void }
  | { kind: 'radio' | 'checkbox'; id?: string; label: string; checked: boolean; disabled?: boolean; onSelect: () => void }
  | { kind: 'heading'; label: string }
  | { kind: 'separator' };

export interface MenuProps {
  id: string;
  label: string;
  open: boolean;
  entries: MenuEntry[];
  trigger: RefObject<HTMLElement | null>;
  /** focusTrigger is true for Escape: focus goes back to the button that opened the menu. */
  onClose: (focusTrigger: boolean) => void;
  className?: string;
  /** Fixed under its trigger instead of placed by CSS: scrolling panes can't clip it. Scrolling closes it. */
  anchored?: boolean;
}

let closeOpenMenu: (() => void) | null = null;
const TYPE_AHEAD_RESET = 1000;
const EDGE = 6;

export function Menu({ id, label, open, entries, trigger, onClose, className, anchored = false }: MenuProps) {
  const node = useRef<HTMLDivElement>(null);
  const typed = useRef({ text: '', at: 0 });
  const close = useRef(onClose);
  close.current = onClose;

  useLayoutEffect(() => {
    const menu = node.current;
    if (!open || !menu) return;
    const mine = () => close.current(false);
    if (closeOpenMenu && closeOpenMenu !== mine) closeOpenMenu();
    closeOpenMenu = mine;
    // Clamp to the viewport through CSSOM, then focus the checked item (or the first).
    menu.style.removeProperty('translate');
    const anchor = anchored ? trigger.current?.getBoundingClientRect() : null;
    if (anchor) {
      menu.style.setProperty('left', `${anchor.left}px`);
      menu.style.setProperty('top', `${anchor.bottom + 2}px`);
    }
    const box = menu.getBoundingClientRect();
    const dx = Math.min(0, innerWidth - EDGE - box.right) + Math.max(0, EDGE - box.left);
    const dy = Math.min(0, innerHeight - EDGE - box.bottom);
    if (dx || dy) menu.style.setProperty('translate', `${dx}px ${dy}px`);
    const list = items(menu);
    (list.find((item) => item.getAttribute('aria-checked') === 'true') ?? list[0])?.focus();
    return () => { if (closeOpenMenu === mine) closeOpenMenu = null; };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      const target = event.target as Node | null;
      if (node.current?.contains(target) || trigger.current?.contains(target)) return;
      close.current(false);
    };
    const scrolled = (event: Event) => {
      if (anchored && !node.current?.contains(event.target as Node | null)) close.current(false);
    };
    document.addEventListener('pointerdown', outside);
    document.addEventListener('scroll', scrolled, true);
    return () => {
      document.removeEventListener('pointerdown', outside);
      document.removeEventListener('scroll', scrolled, true);
    };
  }, [open, trigger, anchored]);

  const keyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const menu = node.current;
    if (!menu) return;
    const list = items(menu);
    const index = list.indexOf(document.activeElement as HTMLButtonElement);
    let next = -1;
    if (event.key === 'ArrowDown') next = (index + 1) % list.length;
    else if (event.key === 'ArrowUp') next = (index + list.length - 1) % list.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = list.length - 1;
    else if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      close.current(true);
      return;
    } else if (event.key === 'Tab') {
      close.current(false);
      return;
    } else if (event.key.length === 1 && !event.metaKey && !event.ctrlKey && !event.altKey) {
      const now = event.timeStamp;
      typed.current = { text: (now - typed.current.at > TYPE_AHEAD_RESET ? '' : typed.current.text) + event.key.toLowerCase(), at: now };
      next = list.findIndex((item) => (item.dataset.label ?? '').toLowerCase().startsWith(typed.current.text));
    }
    if (next >= 0) {
      event.preventDefault();
      list[next]?.focus();
    }
  };

  return (
    <div ref={node} id={id} className={['menu', anchored ? 'is-anchored' : '', className ?? ''].filter(Boolean).join(' ')} role="menu" aria-label={label}
      hidden={!open} onKeyDown={keyDown}>
      <div className="menu-surface">
      {entries.map((entry, index) => {
        if (entry.kind === 'separator') return <hr key={index} role="separator" className="menu-separator" />;
        if (entry.kind === 'heading') return <div key={index} role="presentation" className="menu-heading">{entry.label}</div>;
        const role = entry.kind === 'item' ? 'menuitem' : entry.kind === 'radio' ? 'menuitemradio' : 'menuitemcheckbox';
        const checked = entry.kind === 'item' ? undefined : entry.checked;
        const shortcut = entry.kind === 'item' ? entry.shortcut : undefined;
        return (
          <button key={entry.id ?? index} id={entry.id} type="button" role={role} tabIndex={-1} className="menu-item"
            data-label={entry.label} aria-checked={checked} aria-disabled={entry.disabled || undefined}
            aria-keyshortcuts={shortcut}
            onClick={() => {
              if (entry.disabled) return;
              close.current(false);
              entry.onSelect();
            }}>
            <span className="menu-check" aria-hidden="true">{checked ? '✓' : ''}</span>
            <span className="menu-label">{entry.label}</span>
            {entry.kind === 'item' && entry.shortcutLabel && <span className="menu-shortcut" aria-hidden="true">{entry.shortcutLabel}</span>}
          </button>
        );
      })}
      </div>
    </div>
  );
}

function items(menu: HTMLElement): HTMLButtonElement[] {
  return [...menu.querySelectorAll<HTMLButtonElement>('.menu-item:not([aria-disabled="true"])')];
}
