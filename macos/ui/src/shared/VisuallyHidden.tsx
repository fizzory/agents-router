import type { HTMLAttributes } from 'react';

export function VisuallyHidden({ className, ...rest }: HTMLAttributes<HTMLSpanElement>) {
  return <span className={className ? `visually-hidden ${className}` : 'visually-hidden'} {...rest} />;
}
