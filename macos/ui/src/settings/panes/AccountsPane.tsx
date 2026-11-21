// Accounts: the popover's provider headings, dots, gauge and slot, then an
// enable switch or a Primary/Discovered badge, and an always-visible ⋯ for the
// native menu. Add account… stays under the list in every state. Fresh usage
// slides the bars and rolls the digits as in the popover; reduced motion snaps.
import { useState } from 'react';
import { AccountRow, Button, CopyButton, GroupHeading, Icon, InlineMessage, PaneState, Switch, useFlash, type RowModel } from '../../shared';
import { useSettings } from '../context';
import { accountMatches, type AccountModel } from '../model';

export function AccountsPane({ models, loaded, error, animate = false }: { models: AccountModel[]; loaded: boolean; error?: string; animate?: boolean }) {
  const settings = useSettings();
  const { query } = settings;
  const add = <Button variant="push" className="add-account" onClick={settings.openAddAccount}>Add account…</Button>;
  if (error) return <><PaneState kind="error" detail={error} onRetry={() => { settings.reload('accounts'); }} />{add}</>;
  if (!loaded) return <><PaneState kind="loading" />{add}</>;
  const shown = models.filter((model) => accountMatches(model, query));
  return (
    <>
      {shown.length ? (
        <ul className="account-list settings-accounts" role="list" aria-label="Accounts">
          {shown.flatMap((model, index) => {
            const first = index === 0 || shown[index - 1]?.runtime !== model.runtime;
            const row = <SettingsAccount key={model.key} model={model} index={index} animate={animate} />;
            return first ? [<GroupHeading key={`heading:${model.runtime}`} as="li">{model.provider}</GroupHeading>, row] : [row];
          })}
        </ul>
      ) : query.trim() ? <PaneState kind="no-results" query={query} pane="Accounts" /> : <PaneState kind="empty" text="No accounts yet." />}
      {add}
    </>
  );
}

function SettingsAccount({ model, index, animate }: { model: AccountModel; index: number; animate: boolean }) {
  const settings = useSettings();
  const { api, busyKey } = settings;
  const flash = useFlash<HTMLLIElement>(model.key);
  const { account } = model;
  const [loginFailure, setLoginFailure] = useState<string | null>(null);
  const login = async () => {
    const result = await api.accountsLogin(model.runtime, account.name);
    setLoginFailure(result.status === 'terminal_failed' ? result.copy_command : null);
  };
  const message = settings.messages.get(model.key);
  const toggle = () => settings.mutate(model.key, 'accounts', () => api.accountsToggle(model.runtime, account.name, !account.enabled));
  const menu = (event: React.MouseEvent | null) => {
    const actions: Record<string, () => unknown> = { login };
    if (account.can_toggle) actions[account.enabled ? 'disable' : 'enable'] = toggle;
    settings.showMenu(event, actions);
  };
  const action = (target: RowModel) => (target.action === 'login' ? login()
    : settings.mutate(model.key, 'accounts', () => api.accountsCheck(target.runtime, account.name))).catch(settings.report);
  const badge = model.toggle.kind === 'badge' ? model.toggle : null;
  const trailing = (
    <span className="account-trailing">
      <span className="account-toggle">{badge ? (
        <span className="account-badge" tabIndex={0} role="note" data-tooltip={badge.reason} aria-label={`${badge.text === 'primary' ? 'Primary' : 'Discovered'}. ${badge.reason}`}>
          {badge.text === 'primary' ? 'Primary' : 'Discovered'}
        </span>
      ) : (
        <Switch checked={account.enabled} small label={`${model.provider} ${account.name}`} locked={!!busyKey} busy={busyKey === model.key}
          onChange={() => { toggle().catch(settings.report); }} />
      )}</span>
      <Button variant="icon" className="account-more" aria-label={`More for ${model.provider} ${account.name}`} tooltip="More"
        onClick={(event) => {
          // Keyboard users get the menu at the button; the native menu opens at the pointer.
          event.currentTarget.focus();
          menu(null);
        }}><Icon name="more" /></Button>
    </span>
  );
  const below = (
    <>
      {model.reason && model.state ? <span className="account-reason">{model.reason}</span> : null}
      {message ? <InlineMessage id={`${model.key}-error`} tone={message.tone}>{message.text}</InlineMessage> : null}
      {loginFailure && <InlineMessage tone="error"><span className="login-failure">
        <span>Couldn't open Terminal.</span><CopyButton key={loginFailure} variant="text" value={loginFailure} copy={api.copyText} onError={settings.report} />
      </span></InlineMessage>}
    </>
  );
  return (
    <AccountRow model={{ ...model, name: account.name }} index={index} animate={animate} active reduced={settings.reduced}
      className={['settings-account', flash.on ? 'setting-flash' : ''].filter(Boolean).join(' ')} rowRef={flash.ref}
      onAnimationEnd={flash.onAnimationEnd} onAction={action} tabIndex={() => 0} register={() => () => {}} onFocusPart={() => () => {}}
      trailing={trailing} below={below} onContextMenu={(event) => menu(event)} />
  );
}
