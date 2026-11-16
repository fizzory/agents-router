// Provider and settings group headings: sentence case, 11px/600, muted.
import type { ReactNode } from 'react';

export interface GroupHeadingProps {
  children: ReactNode;
  count?: number;
  /** In the popover's list a heading is an li hidden from the accessibility tree: row labels name the provider. */
  as?: 'li' | 'div' | 'h2';
  id?: string;
}

export function GroupHeading({ children, count, as: Tag = 'div', id }: GroupHeadingProps) {
  const listItem = Tag === 'li';
  return (
    <Tag className="group-heading" id={id} role={listItem ? 'presentation' : undefined} aria-hidden={listItem || undefined}>
      <span className="group-heading-text">{children}</span>
      {count !== undefined && <span className="group-heading-count">{count}</span>}
    </Tag>
  );
}
