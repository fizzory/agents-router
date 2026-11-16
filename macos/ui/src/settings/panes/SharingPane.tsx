// Sharing: the off banner, the sharing settings (User scope), Needs your choice,
// the items grid (a switch per editable cell, otherwise a copy button) and the
// file-only settings. MCP servers and Hooks switch kinds at once.
import { Fragment, type ReactNode } from 'react';
import { SHARE_MORE, SHARE_ORDER, shareResultParts } from '../../../logic/panes.js';
import { Banner, CopyButton, Disclosure, Highlight, InlineMessage, PaneState, Switch } from '../../shared';
import type { ConfigRow as Row } from '../api';
import { useSettings } from '../context';
import { has, itemMatches, paneFor, PROVIDER_NAMES, shareRowMatches, sharingModel, type ShareItemModel, type SharingData } from '../model';
import { ConfigRow } from './ConfigRow';

const FIXED: Record<string, string> = {
  'share.enabled': 'agt share enable --dry-run', 'share.kinds': 'agt share kinds --help',
  'share.plugins': 'agt share kinds add plugins --dry-run', 'share.plugin_parts': 'agt share kinds add plugin-parts --dry-run',
};
const KINDS: [string, string][] = [['agents', 'Agents'], ['skills', 'Skills'], ['mcp', 'MCP servers'], ['hooks', 'Hooks'], ['instructions', 'Instructions'], ['plugins', 'Plugins']];

export function SharingPane({ data, error }: { data: SharingData | null; error?: string }) {
  const settings = useSettings();
  const { api, query } = settings;
  if (error) return <PaneState kind="error" detail={error} onRetry={() => { settings.reload('sharing'); }} />;
  if (!data) return <PaneState kind="loading" />;
  const model = sharingModel(data);
  const searching = query.trim() !== '';
  const rows = data.config.settings.filter((row) => paneFor(row) === 'sharing');
  const visible = (row: Row) => !searching || shareRowMatches(row, query);
  const byKey = (key: string) => rows.find((row) => row.key === key);
  const main = (SHARE_ORDER as string[]).map(byKey).filter((row): row is Row => !!row && visible(row));
  const more = (SHARE_MORE as string[]).map(byKey).filter((row): row is Row => !!row && visible(row));
  const fixed = rows.filter((row) => row.kind === 'fixed' && visible(row));
  const listed = new Set([...(SHARE_ORDER as string[]), ...(SHARE_MORE as string[]), ...rows.filter((row) => row.kind === 'fixed').map((row) => row.key)]);
  const other = rows.filter((row) => !listed.has(row.key) && visible(row));
  const waiting = model.waiting.filter((item) => !searching || has(query, item.message, item.item));
  const items = model.items.filter((item) => !searching || itemMatches(item, query));
  const empty = searching && !main.length && !more.length && !fixed.length && !other.length && !waiting.length && !items.length;

  const row = (setting: Row) => {
    const kinds = setting.key === 'share.mcp' || setting.key === 'share.hooks';
    const result = settings.messages.get(`${setting.key}:result`);
    return (
      <Fragment key={setting.key}>
        {setting.key === 'share.watcher' ? <hr className="pane-divider" /> : null}
        {kinds ? <KindRow setting={setting} data={data} enabled={model.enabled} /> : (
          <ConfigRow row={setting} document={data.config} level="user" workspace={null}
            blockedReason={model.enabled || setting.kind === 'fixed' ? undefined : 'Sharing is off.'}
            fixedAction={setting.kind === 'fixed' ? <CopyButton value={FIXED[setting.key] ?? 'agt share status'} copy={api.copyText} onError={settings.report}
              accessibleName={`Copy command: ${FIXED[setting.key] ?? 'agt share status'}`} /> : undefined} />
        )}
        {result ? <InlineMessage tone="result">{result.text}</InlineMessage> : null}
      </Fragment>
    );
  };

  return (
    <div className={model.enabled ? 'sharing' : 'sharing is-off'}>
      {model.state ? <Banner message={model.state} action={<CopyButton value={model.command} label="Copy enable command" copy={api.copyText} onError={settings.report} />} /> : null}
      <div className="sharing-settings">
        {main.map(row)}
        {other.map(row)}
        {more.length ? <Disclosure storeKey="sharing:more" summary={`More options (${more.length})`} forceOpen={searching}>{more.map(row)}</Disclosure> : null}
      </div>
      {waiting.length ? (
        <Disclosure storeKey="sharing:waiting" summary={`Needs your choice (${waiting.length})`} defaultOpen className="waiting">
          {waiting.map((item, index) => (
            <div key={`${item.item ?? ''}:${index}`} className="waiting-row">
              <span className="waiting-message"><Highlight text={item.message} query={query} /></span>
              <CopyButton value={item.command} copy={api.copyText} onError={settings.report} accessibleName={`Copy command: ${item.command}`} />
            </div>
          ))}
        </Disclosure>
      ) : null}
      <ItemsGrid data={data} items={items} enabled={model.enabled} />
      {!model.items.length && !searching ? <PaneState kind="empty" text="Nothing shared yet." /> : null}
      {fixed.length ? <Disclosure storeKey="sharing:file-only" summary={`File only (${fixed.length})`} forceOpen={searching}>{fixed.map(row)}</Disclosure> : null}
      {empty ? <PaneState kind="no-results" query={query} pane="Sharing" /> : null}
    </div>
  );
}

function KindRow({ setting, data, enabled }: { setting: Row; data: SharingData; enabled: boolean }) {
  const settings = useSettings();
  const kind = setting.key.split('.')[1] ?? '';
  const own = setting.levels.user;
  const value = own?.present ? own.value : setting.value;
  const messageKey = setting.key;
  return (
    <ConfigRowShell setting={setting} control={(
      <Switch id={`setting-${setting.key.replaceAll('.', '-')}`} checked={typeof value === 'boolean' ? value : null}
        describedBy={`setting-${setting.key.replaceAll('.', '-')}-description`}
        disabledReason={enabled ? undefined : 'Sharing is off.'} locked={!!settings.busyKey} busy={settings.busyKey === messageKey}
        onChange={(next) => {
          settings.mutate(messageKey, 'sharing', async () => {
            const report = await settings.api.shareKind(kind, next) as Record<string, unknown>;
            const parts = shareResultParts(report) as { text: string; failed: boolean }[];
            settings.setMessage(`${setting.key}:result`, { tone: 'result', text: parts.map((part, index) => (
              <Fragment key={index}>{index ? ', ' : ''}{part.failed ? <span className="is-failed">{part.text}</span> : part.text}</Fragment>)) });
            return report;
          }).catch(settings.report);
        }} />
    )} data={data} />
  );
}

/** The row frame for a sharing setting whose control isn't a config write. */
function ConfigRowShell({ setting, control }: { setting: Row; control: ReactNode; data: SharingData }) {
  const settings = useSettings();
  const id = `setting-${setting.key.replaceAll('.', '-')}`;
  const message = settings.messages.get(setting.key);
  const label = { 'share.mcp': 'MCP servers', 'share.hooks': 'Hooks' }[setting.key] ?? setting.key;
  return (
    <div className="setting-row" data-setting-key={setting.key} data-key={setting.key} tabIndex={-1}
      data-tooltip={[setting.key, setting.description, setting.applies].filter(Boolean).join('\n')}>
      <label className="setting-label" htmlFor={id}><Highlight text={label} query={settings.query} /></label>
      <span className="setting-control">{control}</span>
      <span className="setting-slot" />
      <span id={`${id}-description`} className="visually-hidden">{[setting.key, setting.description, setting.applies].filter(Boolean).join('\n')}</span>
      {message ? <InlineMessage tone={message.tone}>{message.text}</InlineMessage> : null}
    </div>
  );
}

function ItemsGrid({ data, items, enabled }: { data: SharingData; items: ShareItemModel[]; enabled: boolean }) {
  const settings = useSettings();
  const folders = data.listing.folders ?? [];
  if (!items.length || !folders.length) return null;
  const perCli = new Map<string, number>();
  for (const folder of folders) perCli.set(folder.cli, (perCli.get(folder.cli) ?? 0) + 1);
  const header = (label: string) => {
    const folder = folders.find((item) => item.label === label);
    return folder && perCli.get(folder.cli) === 1 ? PROVIDER_NAMES[folder.cli] ?? folder.cli : label;
  };
  const columns = folders.map((folder) => folder.label);
  const groups = KINDS.map(([kind, title]) => [title, items.filter((item) => item.kind === kind)] as const).filter(([, list]) => list.length);
  const others = items.filter((item) => !KINDS.some(([kind]) => kind === item.kind));
  if (others.length) groups.push(['Other', others]);
  return (
    <div className="share-grid-scroll">
      <table className="share-grid" aria-label="Shared items">
        <thead>
          <tr><th scope="col" className="share-name-head"><span className="visually-hidden">Item</span></th>
            {folders.map((folder) => <th key={folder.label} scope="col" data-tooltip={folder.path}>{header(folder.label)}</th>)}</tr>
        </thead>
        {groups.map(([title, list]) => (
          <tbody key={title}>
            <tr className="share-kind"><th colSpan={columns.length + 1} scope="colgroup" className="group-heading">{title}</th></tr>
            {list.map((item) => (
              <tr key={item.id} className="share-item" data-key={item.id}>
                <th scope="row" className="share-item-name" data-tooltip={item.id}><Highlight text={item.name} query={settings.query} /></th>
                {columns.map((label) => {
                  const cell = item.cells.find((entry) => entry.label === label);
                  if (!cell) return <td key={label} />;
                  const where = header(label);
                  return (
                    <td key={label} data-tooltip={cell.reason}>
                      {cell.editable ? (
                        <Switch small checked={cell.included} label={`${item.name} in ${where}`} disabledReason={enabled ? undefined : 'Sharing is off.'}
                          locked={!!settings.busyKey} busy={settings.busyKey === item.id}
                          onChange={(included) => { settings.mutate(item.id, 'sharing', () => settings.api.sharePick(item.id, label, included)).catch(settings.report); }} />
                      ) : (
                        <CopyButton variant="icon" value={cell.command} copy={settings.api.copyText} onError={settings.report}
                          accessibleName={`Copy command to ${cell.included ? 'remove' : 'include'} ${item.name} ${cell.included ? 'from' : 'in'} ${where}`}
                          tooltip={`${cell.reason}\n${cell.command}`} />
                      )}
                    </td>
                  );
                })}
              </tr>
            ))}
            {list.filter((item) => settings.messages.get(item.id)).map((item) => (
              <tr key={`${item.id}:message`} className="share-message"><td colSpan={columns.length + 1}>
                <InlineMessage tone={settings.messages.get(item.id)?.tone ?? 'error'}>{settings.messages.get(item.id)?.text}</InlineMessage>
              </td></tr>
            ))}
          </tbody>
        ))}
      </table>
    </div>
  );
}
