// The app's inline SVG icon set. No icon fonts, SF Symbols or image files.
import type { SVGProps } from 'react';

const PATHS = {
  refresh: <><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" /><path d="M3 3v5h5" /></>,
  gear: <><path d="M12.22 2h-.44a2 2 0 0 0-2 1.72l-.12.87a2 2 0 0 1-1.18 1.52l-.2.09a2 2 0 0 1-1.9-.18l-.7-.53a2 2 0 0 0-2.58.53l-.22.38a2 2 0 0 0 .34 2.62l.7.53a2 2 0 0 1 .72 1.86v.22a2 2 0 0 1-.72 1.86l-.7.53a2 2 0 0 0-.34 2.62l.22.38a2 2 0 0 0 2.58.53l.7-.53a2 2 0 0 1 1.9-.18l.2.09a2 2 0 0 1 1.18 1.52l.12.87a2 2 0 0 0 2 1.72h.44a2 2 0 0 0 2-1.72l.12-.87a2 2 0 0 1 1.18-1.52l.2-.09a2 2 0 0 1 1.9.18l.7.53a2 2 0 0 0 2.58-.53l.22-.38a2 2 0 0 0-.34-2.62l-.7-.53a2 2 0 0 1-.72-1.86v-.22a2 2 0 0 1 .72-1.86l.7-.53a2 2 0 0 0 .34-2.62l-.22-.38a2 2 0 0 0-2.58-.53l-.7.53a2 2 0 0 1-1.9.18l-.2-.09a2 2 0 0 1-1.18-1.52l-.12-.87a2 2 0 0 0-2-1.72Z" /><circle cx="12" cy="12" r="3" /></>,
  more: <><circle cx="5" cy="12" r="1" /><circle cx="12" cy="12" r="1" /><circle cx="19" cy="12" r="1" /></>,
  search: <><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></>,
  reset: <><path d="M3 12a9 9 0 1 0 3-6.7L3 8" /><path d="M3 3v5h5" /></>,
  lock: <><rect x="5" y="11" width="14" height="10" rx="2" /><path d="M8 11V7a4 4 0 0 1 8 0v4" /></>,
  alert: <><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" /><path d="M12 9v4" /><path d="M12 17h.01" /></>,
  copy: <><rect x="9" y="9" width="12" height="12" rx="2" /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" /></>,
  'chevrons-updown': <><path d="m7 15 5 5 5-5" /><path d="m7 9 5-5 5 5" /></>,
  check: <path d="M20 6 9 17l-5-5" />,
  minus: <path d="M5 12h14" />,
  plus: <path d="M5 12h14M12 5v14" />,
  'chevron-right': <path d="m10 7 5 5-5 5" />,
  spinner: <path d="M21 12a9 9 0 1 1-6.22-8.56" />,
} as const;

export type IconName = keyof typeof PATHS;

export interface IconProps extends SVGProps<SVGSVGElement> {
  name: IconName;
}

export function Icon({ name, className, ...rest }: IconProps) {
  return (
    <svg className={className ? `icon ${className}` : 'icon'} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false" {...rest}>
      {PATHS[name]}
    </svg>
  );
}
