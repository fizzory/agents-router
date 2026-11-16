// Activity: changes grouped by day. A row with a key goes to its setting; a row
// with menu actions shows ⋯; a row with neither is plain text.
import { Fragment } from 'react';
import { Icon, InlineMessage, PaneState } from '../../shared';
import type { ConfigShow } from '../api';
import { useSettings } from '../context';
import { filterActivity, paneForKey, type ActivityEntry, type ActivityModel } from '../model';

export function ActivityPane({ data, error, config }: { data: { entries?: ActivityEntry[]; more?: boolean } | null; error?: string; config: ConfigShow | null }) {
  const settings = useSettings();
  const { query } = settings;
  if (error) return <PaneState kind="error" detail={error} onRetry={() => { settings.reload('activity'); }} />;
  if (!data) return <PaneState kind="loading" />;
  const rows = filterActivity(data.entries ?? [], query, config?.settings ?? [], { now: settings.now(), timeZone: settings.timeZone });
  if (!rows.length) return query.trim() ? <PaneState kind="no-results" query={query} pane="Activity" /> : <PaneState kind="empty" text="No changes yet." />;
  return (
    <>
      {rows.map((row, index) => (
        <Fragment key={row.entry.id}>
          {index === 0 || rows[index - 1]?.day !== row.day ? <h2 className="group-heading activity-day">{row.day}</h2> : null}
          <ActivityRow row={row} />
        </Fragment>
      ))}
      {data.more ? <p className="pane-state activity-more">Showing the latest 200 changes.</p> : null}
    </>
  );
}

function menuActions(entry: ActivityEntry, settings: ReturnType<typeof useSettings>) {
  const { api } = settings;
  const workspace = entry.level === 'user' ? null : entry.workspace || entry.family || null;
  const actions: Record<string, () => unknown> = {};
  if (entry.type === 'setting' && entry.key) {
    const key = entry.key;
    actions.revert = () => settings.mutate(entry.id, 'activity', () => api.configRevert({ entry: entry.id, workspace }));
    actions['copy-key'] = () => api.copyText(key);
  }
  if (entry.type === 'refused' && entry.file) { const file = entry.file; actions['open-file'] = () => api.openSettingsFile(file, entry.line || 1); }
  const changes = entry.changes ?? [];
  const first = changes[0]?.item ?? '';
  if (entry.type === 'sync' && changes.length && changes.every((change) => change.item === first && change.ok) && changes.some((change) => change.backup)
    && /^(agents|skills)\//.test(first) && !entry.workspace && !entry.family) {
    actions['undo-sync'] = () => settings.mutate(entry.id, 'activity', () => api.activityUndoSync(entry.id));
  }
  return actions;
}

/** A long value ellipsizes in its middle, keeping its last word (up to 12 characters); the row tooltip has the full text. */
function MiddleText({ text, className }: { text: string; className?: string }) {
  let keep = 0;
  if (text.length > 12) {
    const space = text.lastIndexOf(' ', text.length - 2);
    keep = space >= 0 && text.length - space - 1 <= 12 ? text.length - space - 1 : Math.min(8, Math.floor(text.length / 3));
  }
  return (
    <span className={className ? `middle-text ${className}` : 'middle-text'}>
      <span className="middle-start">{text.slice(0, text.length - keep)}</span>
      {keep ? <span className="middle-end">{text.slice(text.length - keep)}</span> : null}
    </span>
  );
}

function ActivityRow({ row }: { row: ActivityModel }) {
  const settings = useSettings();
  const { entry } = row;
  const actions = menuActions(entry, settings);
  const hasMenu = Object.keys(actions).length > 0;
  const message = settings.messages.get(entry.id);
  const cells = (
    <>
      <time className="activity-time">{row.time}</time>
      <span className="activity-label">{row.label}</span>
      {row.marker ? (
        <span className="activity-marker"><Icon name="alert" />{row.marker}</span>
      ) : (
        <>
          <span className="activity-change">{row.parts ? (
            <><MiddleText text={row.parts[0]} className="is-old" /><span className="activity-arrow"> → </span><MiddleText text={row.parts[1]} /></>
          ) : <MiddleText text={row.change} />}</span>
          <span className="activity-level">{row.level}</span>
        </>
      )}
    </>
  );
  const label = [row.time, row.label, row.marker || row.change, row.marker ? '' : row.level].filter(Boolean).join(', ');
  const go = entry.key ? () => {
    const key = entry.key ?? '';
    settings.navigate({ pane: paneForKey(key), key, level: (entry.level as 'user' | undefined) || 'user',
      workspace: entry.level === 'user' ? null : entry.workspace || entry.family || null }).catch(settings.report);
  } : null;
  return (
    <div className="activity-row" data-key={entry.id} data-tooltip={row.tooltip || undefined}
      onContextMenu={(event) => { if (hasMenu) settings.showMenu(event, actions); }}>
      {go ? <button type="button" className="activity-main" aria-label={label} onClick={go}>{cells}</button>
        : <div className="activity-main" aria-label={label} role="group">{cells}</div>}
      {hasMenu ? <button type="button" className="button button-icon activity-more" aria-label={`More for ${row.label}`} data-tooltip="More"
        onClick={() => settings.showMenu(null, actions)}><Icon name="more" /></button> : <span className="activity-more-space" aria-hidden="true" />}
      {message ? <InlineMessage tone={message.tone}>{message.text}</InlineMessage> : null}
    </div>
  );
}
