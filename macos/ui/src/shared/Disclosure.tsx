// A <details> whose open state is kept by key across refreshes, searches and
// pane switches. Its children stay in the DOM while it is closed.
import { useState, type ReactNode } from 'react';

const store = new Map<string, boolean>();
export const resetDisclosures = () => store.clear();

export interface DisclosureProps {
  storeKey: string;
  summary: ReactNode;
  id?: string;
  defaultOpen?: boolean;
  /** Search forces a matching disclosure open without changing what is stored. */
  forceOpen?: boolean;
  className?: string;
  children: ReactNode;
}

export function Disclosure({ storeKey, summary, id, defaultOpen = false, forceOpen = false, className, children }: DisclosureProps) {
  const [open, setOpen] = useState(() => store.get(storeKey) ?? defaultOpen);
  const shown = open || forceOpen;
  return (
    <details id={id} className={className ? `disclosure ${className}` : 'disclosure'} open={shown}
      onToggle={(event) => {
        const next = event.currentTarget.open;
        if (next === shown) return;
        store.set(storeKey, next);
        setOpen(next);
      }}>
      <summary className="disclosure-summary">{summary}</summary>
      <div className="disclosure-body">{children}</div>
    </details>
  );
}
