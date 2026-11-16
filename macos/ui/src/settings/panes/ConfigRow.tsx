// One row of the settings document: the control for its kind and the trailing
// slot (reset, source tag, Open file or lock). Every such row carries data-setting-key.
import { useRef, type ReactNode } from 'react';
import { autoCopyKinds, autoCopyLabel, autoCopyValue } from '../../../logic/panes.js';
import { brokenFile, cliValue, fileTarget } from '../../../logic/settings.js';
import { Button, Icon, NumberField, PopupButton, SettingRow, SourceTag, Switch, TextField, type MenuEntry } from '../../shared';
import type { ConfigRow as Row, ConfigShow, Level } from '../api';
import { useSettings } from '../context';
import { displayValue, LEVEL_NAMES, labelFor, paneFor, rowState, SHARE_NAMES, valueLabel } from '../model';

export interface ConfigRowProps {
  row: Row;
  document: ConfigShow;
  /** Sharing rows always read and write User settings. */
  level?: Level;
  workspace?: string | null;
  /** Fixed rows: Open file (settings panes) or Copy command (Sharing). */
  fixedAction?: ReactNode;
  label?: string;
  /** Overrides editability with a reason, for example Sharing is off. */
  blockedReason?: string;
}

export const controlId = (key: string) => `setting-${key.replaceAll('.', '-')}`;

export function ConfigRow({ row, document, level, workspace, fixedAction, label: shownLabel, blockedReason }: ConfigRowProps) {
  const settings = useSettings();
  const scope = level ? { level, workspace: workspace ?? null } : settings.scope;
  const view = rowState(row, scope.level, document);
  const id = controlId(row.key);
  const descriptionId = `${id}-description`;
  const errorId = `${id}-error`;
  const label = shownLabel ?? SHARE_NAMES[row.key] ?? labelFor(row.key);
  const message = settings.messages.get(row.key);
  const invalid = message?.tone === 'error';
  const writing = settings.busyKey === row.key;
  const locked = !!settings.busyKey;
  const broken = !!document.error;
  const reason = blockedReason ?? (view.editable ? undefined : broken ? 'Fix settings.toml first.' : view.chip?.endsWith(' only') ? `${view.chip}.` : 'Can’t be changed here.');
  const inherited = !view.own?.present;
  // The revision a focused draft started from; undefined when no draft is open.
  const draftRevision = useRef<string | null | undefined>(undefined);
  const save = (value: string | null, remove = false) => {
    const revision = draftRevision.current;
    draftRevision.current = undefined;
    settings.save(row, value, { remove, document, scope, revision }).catch(settings.report);
  };
  const common = { describedBy: descriptionId, inherited, disabledReason: reason, locked, invalid, errorId };
  const onDraftStart = () => { draftRevision.current = view.own?.revision ?? null; };
  const onDraftEnd = () => { draftRevision.current = undefined; };

  let control: ReactNode;
  let wide = false;
  if (row.kind === 'int' || row.kind === 'float') {
    control = <NumberField id={id} label={label} kind={row.kind} minimum={row.minimum} value={typeof view.value === 'number' ? view.value : null}
      source={LEVEL_NAMES[row.source as Level] ?? row.source} onCommit={(text) => save(text)} onDraftStart={onDraftStart} onDraftEnd={onDraftEnd} {...common} />;
  } else if (row.kind === 'bool') {
    control = <Switch id={id} checked={typeof view.value === 'boolean' ? view.value : null} describedBy={descriptionId} disabledReason={reason}
      locked={locked} invalid={invalid} errorId={errorId} onChange={(next) => save(String(next))} />;
  } else if (row.kind === 'choice') {
    // Menu items and the button show readable labels; the write stays agt's raw choice.
    const entries: MenuEntry[] = (row.choices ?? []).map((choice) => ({ kind: 'radio', label: valueLabel(row.key, choice) ?? String(choice),
      checked: choice === view.value, onSelect: () => save(cliValue(choice)) }));
    control = <PopupButton id={id} label={label} text={valueLabel(row.key, view.value) ?? (displayValue(view.value) || 'Not set')} entries={entries} {...common} />;
  } else if (row.kind === 'auto_copy') {
    const kinds = autoCopyKinds(view.value) as string[];
    const toggle = (kind: string) => save(autoCopyValue(kinds.includes(kind) ? kinds.filter((item) => item !== kind) : [...kinds, kind]));
    const entries: MenuEntry[] = [
      { kind: 'radio', label: 'Every CLI', checked: kinds.length === 2, onSelect: () => save('on') },
      { kind: 'radio', label: 'Wait for a choice', checked: kinds.length === 0, onSelect: () => save('off') },
      { kind: 'separator' },
      { kind: 'checkbox', label: 'Agents', checked: kinds.includes('agents'), onSelect: () => toggle('agents') },
      { kind: 'checkbox', label: 'Skills', checked: kinds.includes('skills'), onSelect: () => toggle('skills') },
    ];
    control = <PopupButton id={id} label={label} text={autoCopyLabel(view.value)} entries={entries} {...common} />;
  } else if (row.kind === 'fixed') {
    control = (
      <span className="fixed-value">
        <output id={id} aria-describedby={descriptionId}>{displayValue(view.value) || 'Not set'}</output>
        <Icon name="lock" aria-label="Set in a file only" role="img" aria-hidden={false} />
      </span>
    );
  } else {
    wide = true;
    control = <TextField id={id} variant={row.kind === 'names' ? 'names' : 'text'} value={displayValue(view.value)} onCommit={(text) => save(text)} onDraftStart={onDraftStart} onDraftEnd={onDraftEnd} {...common} />;
  }

  let slot: ReactNode = null;
  if (row.kind === 'fixed') slot = fixedAction ?? <OpenFile row={row} level={scope.level} label={label} />;
  else if (broken) slot = <span className="slot-lock" role="img" aria-label="Locked: settings.toml has an error" data-tooltip="settings.toml has an error."><Icon name="lock" /></span>;
  else if (view.chip) {
    const chipLevel = view.chipLevel;
    slot = <SourceTag text={view.chip} tooltip={view.chipTooltip} label={`${view.label}, ${view.chip}. ${view.chipTooltip}`}
      onNavigate={chipLevel ? () => { settings.navigate({ pane: paneFor(row), level: chipLevel, workspace: scope.workspace, key: row.key }).catch(settings.report); } : undefined} />;
  } else if (view.reset) {
    slot = <Button variant="icon" className="reset-button" aria-label={`Reset ${label}`} tooltip={`Reset ${label}`}
      disabledReason={locked ? undefined : reason} onClick={() => { if (!locked) save(null, true); }}><Icon name="reset" /></Button>;
  }

  return (
    <SettingRow settingKey={row.key} rowKey={row.key} label={label} query={settings.query} controlId={row.kind === 'fixed' ? undefined : id}
      descriptionId={descriptionId} description={view.tooltip} control={control} slot={slot} writing={writing} wide={wide}
      message={message ? { ...message, id: errorId } : null}
      onContextMenu={(event) => { event.preventDefault(); settings.api.showSettingMenu(row.key).catch(settings.report); }} />
  );
}

function OpenFile({ row, level, label }: { row: Row; level: Level; label: string }) {
  const settings = useSettings();
  const target = fileTarget(row, level) as { file: string; line: number };
  return (
    <Button variant="text" aria-label={`Open ${label} in settings.toml at line ${target.line}`} tooltip={`${target.file}:${target.line}`}
      onClick={() => { settings.api.openSettingsFile(target.file, target.line).catch(settings.report); }}>Open file</Button>
  );
}

/** "settings.toml has an error on line N." with agt's diagnostic and Open settings.toml. */
export function BrokenBanner({ document }: { document: ConfigShow | null }) {
  const settings = useSettings();
  const broken = document ? brokenFile(document) as { message: string; file?: string; line: number } | null : null;
  if (!broken) return null;
  return (
    <div className="banner is-error" role="alert">
      <div className="banner-text">
        <span className="banner-message">settings.toml has an error on line {broken.line}.</span>
        <span className="banner-detail">{broken.message}</span>
      </div>
      {broken.file ? <Button variant="push" onClick={() => { settings.api.openSettingsFile(broken.file ?? '', broken.line).catch(settings.report); }}>Open settings.toml</Button> : null}
    </div>
  );
}
