// One setting: label, control, then a trailing slot with exactly one of reset,
// source tag, Open file or lock. The description sits in a hidden element the
// control names with aria-describedby; the tooltip shows the same text.
import type { MouseEvent, ReactNode } from 'react';
import { useFlash } from './flash';
import { Spinner } from './Spinner';
import { useReducedMotion } from './motion';

export type MessageTone = 'error' | 'warn' | 'stale' | 'result';
export interface RowMessage { tone: MessageTone; text: ReactNode }

export function InlineMessage({ id, tone, children }: { id?: string; tone: MessageTone; children: ReactNode }) {
  return <div id={id} className={`inline-message is-${tone}`} role={tone === 'error' ? 'alert' : 'status'}>{children}</div>;
}

/** Wraps each case-insensitive match of query in <mark>. */
export function Highlight({ text, query }: { text: string; query?: string }) {
  const needle = (query ?? '').trim().toLowerCase();
  if (!needle) return <>{text}</>;
  const parts: ReactNode[] = [];
  const lower = text.toLowerCase();
  let start = 0;
  let at = lower.indexOf(needle);
  while (at >= 0) {
    parts.push(text.slice(start, at), <mark key={at}>{text.slice(at, at + needle.length)}</mark>);
    start = at + needle.length;
    at = lower.indexOf(needle, start);
  }
  parts.push(text.slice(start));
  return <>{parts}</>;
}

export interface SourceTagProps {
  text: string;
  tooltip: string;
  /** Navigates to the winning scope and key; without it the tag is static. */
  onNavigate?: () => void;
  label?: string;
}

/** The tag ellipsizes in its 84px slot, so its tooltip always starts with the full text. */
export function SourceTag({ text, tooltip, onNavigate, label }: SourceTagProps) {
  const help = tooltip && tooltip !== text ? `${text}\n${tooltip}` : text;
  if (onNavigate) {
    return <button type="button" className="source-tag" data-tooltip={help} aria-label={label} onClick={onNavigate}><span className="source-tag-text">{text}</span></button>;
  }
  return <span className="source-tag is-static" tabIndex={0} role="note" data-tooltip={help} aria-label={label}><span className="source-tag-text">{text}</span></span>;
}

export interface SettingRowProps {
  /** The config key, for data-setting-key. Only the settings document's rows set it. */
  settingKey?: string;
  /** Flash and focus target key (navigation, undo, source tags). */
  rowKey: string;
  label: string;
  query?: string;
  controlId?: string;
  descriptionId: string;
  description: string;
  control: ReactNode;
  slot?: ReactNode;
  writing?: boolean;
  wide?: boolean;
  message?: (RowMessage & { id: string }) | null;
  onContextMenu?: (event: MouseEvent) => void;
  className?: string;
}

export function SettingRow({ settingKey, rowKey, label, query, controlId, descriptionId, description, control, slot, writing = false, wide = false,
  message, onContextMenu, className }: SettingRowProps) {
  const reduced = useReducedMotion();
  const flash = useFlash(rowKey);
  const classes = ['setting-row', wide ? 'is-wide' : '', flash.on ? 'setting-flash' : '', className ?? ''].filter(Boolean).join(' ');
  return (
    <div ref={flash.ref} className={classes} data-setting-key={settingKey} data-key={rowKey} tabIndex={-1} data-tooltip={description}
      onContextMenu={onContextMenu} onAnimationEnd={flash.onAnimationEnd}>
      <label className="setting-label" htmlFor={controlId}><Highlight text={label} query={query} /></label>
      <span className="setting-control">{control}</span>
      <span className="setting-slot">{writing ? <Spinner enabled={!reduced} /> : slot}</span>
      <span id={descriptionId} className="visually-hidden">{description}</span>
      {message && <InlineMessage id={message.id} tone={message.tone}>{message.text}</InlineMessage>}
    </div>
  );
}
