// A message at the top of a pane with at most one action, and the pane states:
// loading, read error with Retry, empty, no results.
import type { ReactNode } from 'react';
import { Button } from './Button';

export interface BannerProps {
  tone?: 'info' | 'error';
  message: ReactNode;
  detail?: ReactNode;
  action?: { label: string; onClick: () => void; busy?: boolean } | ReactNode;
  id?: string;
}

const isAction = (value: unknown): value is { label: string; onClick: () => void; busy?: boolean } =>
  !!value && typeof value === 'object' && 'label' in value && 'onClick' in value;

export function Banner({ tone = 'info', message, detail, action, id }: BannerProps) {
  return (
    <div id={id} className={`banner is-${tone}`} role={tone === 'error' ? 'alert' : undefined}>
      <div className="banner-text">
        <span className="banner-message">{message}</span>
        {detail ? <span className="banner-detail">{detail}</span> : null}
      </div>
      {isAction(action) ? <Button variant="push" busy={action.busy} onClick={action.onClick}>{action.label}</Button> : action}
    </div>
  );
}

export type PaneStateProps =
  | { kind: 'loading'; text?: string }
  | { kind: 'error'; detail: string; onRetry: () => void; message?: string }
  | { kind: 'empty'; text: string; action?: ReactNode }
  | { kind: 'no-results'; query: string; pane: string };

export function PaneState(props: PaneStateProps) {
  if (props.kind === 'error') {
    return <Banner tone="error" message={props.message ?? "Can't read agt."} detail={props.detail} action={{ label: 'Retry', onClick: props.onRetry }} />;
  }
  if (props.kind === 'loading') return <p className="pane-state" role="status">{props.text ?? 'Loading…'}</p>;
  if (props.kind === 'empty') return <div className="pane-state"><span>{props.text}</span>{props.action}</div>;
  return <p className="pane-state" role="status">No results for “{props.query.trim()}” in {props.pane}.</p>;
}
