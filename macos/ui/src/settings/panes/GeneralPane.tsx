// General: the install banner, Jobs (the top-level keys), App (Open at login,
// updates, the command line tool) and, on test builds only, Test build last.
import type { Dispatch, SetStateAction } from 'react';
import { installState } from '../../../logic/settings.js';
import { Banner, Button, CopyButton, Disclosure, GroupHeading, Highlight, PaneState, SettingRow, SourceTag, Switch } from '../../shared';
import type { ConfigShow, Envelope, InstallStatus } from '../api';
import { useSettings } from '../context';
import { errorMessage, matches, paneFor, testBuildMatches, type TestRow } from '../model';
import { BrokenBanner, ConfigRow } from './ConfigRow';

export interface LoginItem { value: boolean | null; readable: boolean; busy: boolean; error?: string }

interface InstallView { problem: { message: string; action: string } | null; version: string | null; owned: boolean; uninstallDisabled: boolean; uninstallReason: string }

export interface GeneralPaneProps {
  config: ConfigShow | null;
  configError: string | null;
  snapshot: Envelope | null;
  install: InstallStatus | null;
  installError: string | null;
  readInstall: () => Promise<void>;
  login: LoginItem;
  setLogin: Dispatch<SetStateAction<LoginItem>>;
  testRows: TestRow[];
}

export function GeneralPane({ config, configError, snapshot, install, installError, readInstall, login, setLogin, testRows }: GeneralPaneProps) {
  const settings = useSettings();
  const { api, query } = settings;
  const searching = query.trim() !== '';
  const model = installState(snapshot ?? {}) as InstallView;
  const problem = model.problem ?? (install?.setup_unknown ? { message: 'Setup status unknown.', action: 'Check again' } : null);
  const status = install ?? {} as Partial<InstallStatus>;
  const version = 'version' in status ? status.version : model.version;
  const owned = model.owned && (!('version' in status) || !!status.version);
  const action = (name: string) => { api.installAction(name).catch(settings.report); };
  const rows = (config?.settings ?? []).filter((row) => paneFor(row) === 'general' && (!searching || matches(row, query)));
  const testMatch = searching && testBuildMatches(testRows, query);

  const toggleLogin = async (next: boolean) => {
    setLogin((current) => ({ ...current, value: next, busy: true, error: undefined }));
    try {
      const value = await api.setOpenAtLogin(next);
      setLogin({ value, readable: true, busy: false });
    } catch (error) {
      setLogin({ value: !next, readable: true, busy: false, error: `Couldn't change Open at login. ${errorMessage(error)}` });
    }
  };

  return (
    <>
      {!searching && installError ? (
        <Banner tone="error" message="Can't read install status." detail={installError} action={{ label: 'Retry', onClick: () => { readInstall(); } }} />
      ) : !searching && problem ? (
        <Banner message={problem.message} action={{ label: problem.action,
          onClick: () => action(problem.action === 'Repair' ? 'repair' : problem.action === 'Install' ? 'install' : 'check-setup') }} />
      ) : null}
      <BrokenBanner document={config} />
      {!config ? (configError ? <PaneState kind="error" detail={configError} onRetry={() => { settings.reload('general'); }} />
        : <PaneState kind="loading" text="Loading settings…" />) : null}
      {config && rows.length ? (
        <section className="settings-group" aria-labelledby="group-jobs">
          <GroupHeading as="h2" id="group-jobs">Jobs</GroupHeading>
          {rows.map((row) => <ConfigRow key={row.key} row={row} document={config} />)}
        </section>
      ) : null}
      {searching && config && !rows.length && !testMatch ? <PaneState kind="no-results" query={query} pane="General" /> : null}
      {!searching || testMatch ? (
        <footer id="general-footer" className="settings-group">
          {!searching ? (
            <>
              <GroupHeading as="h2" id="group-app">App</GroupHeading>
              <SettingRow rowKey="app.open_at_login" label="Open at login" controlId="open-at-login" descriptionId="open-at-login-description"
                description="Start Agents Router when you log in."
                control={<Switch id="open-at-login" checked={login.value} busy={login.busy} describedBy="open-at-login-description"
                  disabledReason={login.readable ? undefined : "Can't read the login item."} onChange={(next) => { toggleLogin(next); }} />}
                message={login.error ? { tone: 'error', text: login.error, id: 'open-at-login-error' } : null} />
              <SettingRow rowKey="app.updates" label="Updates" descriptionId="updates-description" description="Look for a newer Agents Router."
                control={<Button id="check-updates" variant="push" onClick={() => { api.checkForUpdates().catch(settings.report); }}>Check for Updates…</Button>} />
              <div id="install-group" className="setting-row" data-key="app.cli">
                <span className="setting-label">Command line tool</span>
                <span className="setting-control cli-control">
                  <output id="version">{version ? `agt ${version}` : problem?.action === 'Install' ? 'Not installed' : 'Unavailable'}</output>
                  {owned ? <Button id="uninstall" variant="text" disabledReason={model.uninstallDisabled ? model.uninstallReason : undefined}
                    onClick={() => action('uninstall')}>Uninstall agt…</Button> : null}
                  {status.can_restore ? <Button id="restore-cli" variant="text" onClick={() => action('restore')}>Restore previous agt</Button> : null}
                </span>
              </div>
            </>
          ) : null}
          {testRows.length > 0 && (!searching || testMatch) ? (
            <Disclosure id="test-build" storeKey="general:test-build" summary="Test build" forceOpen={testMatch}>
              {testRows.filter((row) => !searching || testBuildMatches([row], query)).map((row) => (
                <div key={row.key} className="setting-row test-build-row" data-key={`test-build.${row.key}`}>
                  <span className="setting-label"><Highlight text={row.label} query={query} /></span>
                  <span className="setting-control path-value">
                    <span className="path-text" data-tooltip={row.full}><Highlight text={row.value} query={query} /></span>
                    {row.chip ? <SourceTag text={row.chip} tooltip={row.chip === 'Flag' ? 'Set by a --test flag' : 'Set by an environment variable'} /> : null}
                  </span>
                  <span className="setting-slot">
                    <CopyButton variant="icon" value={row.path} copy={api.copyText} accessibleName={row.copyLabel} tooltip={row.copyLabel} onError={settings.report} />
                  </span>
                </div>
              ))}
            </Disclosure>
          ) : null}
        </footer>
      ) : null}
    </>
  );
}
