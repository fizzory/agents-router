// Claude, Codex, Antigravity and Kiro: their settings grouped by key suffix,
// file-only settings in a disclosure with a count.
import { FILE_ONLY, GROUP_ORDER, groupFor } from '../../../logic/settings.js';
import { Disclosure, GroupHeading, PaneState } from '../../shared';
import type { ConfigShow } from '../api';
import { useSettings } from '../context';
import { matches, PANE_TITLES, paneFor, type PaneId } from '../model';
import { BrokenBanner, ConfigRow } from './ConfigRow';

export function ProviderPane({ provider, config, configError }: { provider: PaneId; config: ConfigShow | null; configError: string | null }) {
  const settings = useSettings();
  const { query } = settings;
  const searching = query.trim() !== '';
  if (!config) {
    return configError ? <PaneState kind="error" detail={configError} onRetry={() => { settings.reload(provider); }} />
      : <PaneState kind="loading" text="Loading settings…" />;
  }
  const rows = config.settings.filter((row) => paneFor(row) === provider && (!searching || matches(row, query)));
  const groups = new Map<string, typeof rows>();
  for (const row of rows) {
    const group = groupFor(row) as string;
    groups.set(group, [...(groups.get(group) ?? []), row]);
  }
  const fileOnly = groups.get(FILE_ONLY) ?? [];
  return (
    <>
      <BrokenBanner document={config} />
      {(GROUP_ORDER as string[]).filter((group) => groups.has(group)).map((group) => {
        const id = `group-${provider}-${group.toLowerCase().replaceAll(' ', '-')}`;
        return (
          <section key={group} className="settings-group" aria-labelledby={id}>
            <GroupHeading as="h2" id={id}>{group}</GroupHeading>
            {groups.get(group)?.map((row) => <ConfigRow key={row.key} row={row} document={config} />)}
          </section>
        );
      })}
      {fileOnly.length ? (
        <Disclosure storeKey={`${provider}:file-only`} summary={`File only (${fileOnly.length})`} forceOpen={searching}>
          {fileOnly.map((row) => <ConfigRow key={row.key} row={row} document={config} />)}
        </Disclosure>
      ) : null}
      {searching && !rows.length ? <PaneState kind="no-results" query={query} pane={PANE_TITLES[provider]} /> : null}
    </>
  );
}
