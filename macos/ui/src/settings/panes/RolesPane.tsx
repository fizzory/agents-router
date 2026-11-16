// Roles: a switch, the role name, provider chips and a source tag per row.
// Ordered roles (edit, full) number their chips, which move by drag, ⌥←/⌥→
// or the native menu; every chip opens the model and effort menu.
import { useLayoutEffect, useRef, useState, type DragEvent, type KeyboardEvent } from 'react';
import { moveProvider, orderedRole, roleChoices, roleName, roleSource } from '../../../logic/panes.js';
import { displayValue } from '../../../logic/settings.js';
import { Button, Highlight, Icon, InlineMessage, Menu, PaneState, SourceTag, Switch, useAnnounce, useFlash, type MenuEntry } from '../../shared';
import type { ConfigShow } from '../api';
import { useSettings } from '../context';
import { errorMessage, roleMatches, type Role, type RolesDocument } from '../model';
import { BrokenBanner } from './ConfigRow';

type Provider = Role['providers'][number];
interface Choice { label: string; value: string | null; selected: boolean }

export function RolesPane({ document, error, config }: { document: RolesDocument | null; error?: string; config: ConfigShow | null }) {
  const settings = useSettings();
  const { query } = settings;
  if (error) return <PaneState kind="error" detail={error} onRetry={() => { settings.reload('roles'); }} />;
  if (!document) return <PaneState kind="loading" />;
  const roles = document.roles.filter((role) => roleMatches(role, query));
  return (
    <>
      <BrokenBanner document={config} />
      {document.error ? <div className="banner is-error" role="alert"><span className="banner-message">{errorMessage(document)}</span></div> : null}
      {roles.map((role) => <RoleRow key={role.name} role={role} locked={!!document.error} />)}
      {!document.roles.length && !query ? (
        <PaneState kind="empty" text="No roles yet." action={<TerminalButton label="Set up in Terminal" run={() => settings.api.setupRoles()} />} />
      ) : null}
      {query.trim() && !roles.length ? <PaneState kind="no-results" query={query} pane="Roles" /> : null}
    </>
  );
}

/** A Terminal handoff with one busy guard: a second click while it opens does nothing. */
export function TerminalButton({ label, run, variant = 'push', ariaLabel }: { label: string; run: () => Promise<unknown>; variant?: 'push' | 'text' | 'inline'; ariaLabel?: string }) {
  const settings = useSettings();
  const [busy, setBusy] = useState(false);
  return (
    <Button variant={variant} busy={busy} aria-label={busy ? 'Opening Terminal…' : ariaLabel} onClick={async () => {
      setBusy(true);
      try { await run(); } catch (error) { settings.report(error); } finally { setBusy(false); }
    }}>{busy ? 'Opening Terminal…' : label}</Button>
  );
}

function RoleRow({ role, locked }: { role: Role; locked: boolean }) {
  const settings = useSettings();
  const announce = useAnnounce();
  const key = `roles.${role.name}`;
  const flash = useFlash(key);
  const blocked = locked || !!settings.busyKey;
  const ordered = orderedRole(role) as boolean;
  const source = roleSource(role, settings.scope.level) as { text: string; key: string; value: unknown; level: string; file?: string; line?: number } | null;
  const message = settings.messages.get(key);
  const [focusIndex, setFocusIndex] = useState(0);
  const chips = useRef(new Map<string, HTMLButtonElement>());
  const before = useRef<Map<string, DOMRect> | null>(null);
  const focusEntry = useRef<string | null>(null);
  const drag = useRef<number | null>(null);
  const order = role.providers.map((provider) => provider.entry).join('\n');

  // FLIP: chips slide from their old place after a reorder (instant under reduced motion).
  useLayoutEffect(() => {
    const old = before.current;
    before.current = null;
    if (focusEntry.current) { chips.current.get(focusEntry.current)?.focus(); focusEntry.current = null; }
    if (!old || settings.reduced) return;
    for (const [entry, node] of chips.current) {
      const previous = old.get(entry);
      if (!previous) continue;
      const now = node.getBoundingClientRect();
      const dx = previous.left - now.left;
      const dy = previous.top - now.top;
      if (!dx && !dy) continue;
      node.classList.remove('chip-moving');
      node.style.setProperty('transform', `translate(${dx}px, ${dy}px)`);
      requestAnimationFrame(() => { node.classList.add('chip-moving'); node.style.removeProperty('transform'); });
    }
  }, [order]);

  const move = async (from: number, to: number) => {
    const change = moveProvider(role, from, to) as { edit: unknown; announcement: string } | null;
    if (!change || blocked) return;
    const entry = role.providers[from]?.entry ?? null;
    before.current = new Map([...chips.current].map(([name, node]) => [name, node.getBoundingClientRect()]));
    const result = await settings.roleWrite(key, change.edit);
    const refused = !result || (typeof result === 'object' && ('error' in result && (result as { error: unknown }).error || 'needs_user_words' in result && (result as { needs_user_words: unknown }).needs_user_words));
    if (refused) { before.current = null; return; }
    focusEntry.current = entry;
    setFocusIndex(to);
    announce(change.announcement);
  };

  const keyDown = (index: number) => (event: KeyboardEvent<HTMLButtonElement>) => {
    const keys = ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'];
    if (!keys.includes(event.key)) return;
    event.preventDefault();
    const last = role.providers.length - 1;
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? last : index + (['ArrowLeft', 'ArrowUp'].includes(event.key) ? -1 : 1);
    const target = Math.max(0, Math.min(last, next));
    if (event.altKey && ordered) { move(index, target).catch(settings.report); return; }
    setFocusIndex(target);
    chips.current.get(role.providers[target]?.entry ?? '')?.focus();
  };

  return (
    <div ref={flash.ref} className={['role-row', flash.on ? 'setting-flash' : ''].filter(Boolean).join(' ')} data-key={key} data-role={role.name}
      tabIndex={-1} data-tooltip={role.use_when || role.name} onAnimationEnd={flash.onAnimationEnd}>
      <Switch checked={role.enabled} label={`${roleName(role.name)} role`} locked={!!settings.busyKey} disabledReason={locked ? 'Fix the roles document first.' : undefined}
        onChange={(enabled) => { settings.roleWrite(key, { kind: 'enable', role: role.name, enabled }).catch(settings.report); }} />
      <span className="role-name"><Highlight text={role.name} query={settings.query} /></span>
      <div className="provider-chips" role="group" aria-label={`${roleName(role.name)} providers`}>
        {role.providers.map((provider, index) => (
          <Chip key={provider.entry} role={role} provider={provider} index={index} ordered={ordered} blocked={blocked}
            tabIndex={index === Math.min(focusIndex, role.providers.length - 1) ? 0 : -1}
            register={(node) => { if (node) chips.current.set(provider.entry, node); else chips.current.delete(provider.entry); }}
            onFocus={() => setFocusIndex(index)} onKeyDown={keyDown(index)}
            onMenu={(event) => {
              const actions: Record<string, () => unknown> = {};
              if (ordered && !blocked) {
                if (index > 0) { actions['move-front'] = () => move(index, 0); actions['move-up'] = () => move(index, index - 1); }
                if (index < role.providers.length - 1) actions['move-down'] = () => move(index, index + 1);
              }
              if (Object.keys(actions).length) settings.showMenu(event, actions); else event.preventDefault();
            }}
            drag={{
              start: (event) => { drag.current = index; event.dataTransfer.effectAllowed = 'move'; event.dataTransfer.setData('text/plain', provider.entry); },
              over: (event) => { if (drag.current !== null && ordered) { event.preventDefault(); event.dataTransfer.dropEffect = 'move'; } },
              drop: (event) => { event.preventDefault(); const from = drag.current; drag.current = null; if (from !== null) move(from, index).catch(settings.report); },
              end: () => { drag.current = null; },
            }} />
        ))}
      </div>
      <span className="setting-slot">
        {source ? <SourceTag text={source.text} tooltip={`${source.key}: ${displayValue(source.value)}\n${source.file}:${source.line || 1}`}
          onNavigate={() => { settings.navigate({ pane: 'roles', level: source.level as 'user', workspace: settings.scope.workspace, key }).catch(settings.report); }} /> : null}
      </span>
      {message ? <InlineMessage id={`${key}-error`} tone={message.tone}>{message.text}</InlineMessage> : null}
    </div>
  );
}

interface ChipProps {
  role: Role; provider: Provider; index: number; ordered: boolean; blocked: boolean; tabIndex: number;
  register: (node: HTMLButtonElement | null) => void; onFocus: () => void; onKeyDown: (event: KeyboardEvent<HTMLButtonElement>) => void;
  onMenu: (event: React.MouseEvent) => void;
  drag: { start: (event: DragEvent) => void; over: (event: DragEvent) => void; drop: (event: DragEvent) => void; end: () => void };
}

function Chip({ role, provider, index, ordered, blocked, tabIndex, register, onFocus, onKeyDown, onMenu, drag }: ChipProps) {
  const settings = useSettings();
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement | null>(null);
  const choices = roleChoices(provider) as { models: Choice[]; efforts: Choice[] };
  const write = (kind: 'model' | 'effort', value: string | null) => {
    settings.roleWrite(`roles.${role.name}`, { kind, entry: provider.entry, value }).catch(settings.report);
  };
  const entries: MenuEntry[] = [
    { kind: 'heading', label: 'Applies to every role' },
    { kind: 'heading', label: 'Model' },
    ...choices.models.map((choice): MenuEntry => ({ kind: 'radio', label: choice.label, checked: choice.selected, onSelect: () => write('model', choice.value) })),
    { kind: 'separator' },
    { kind: 'heading', label: 'Effort' },
    ...choices.efforts.map((choice): MenuEntry => ({ kind: 'radio', label: choice.label, checked: choice.selected, onSelect: () => write('effort', choice.value) })),
  ];
  const count = role.providers.length;
  const name = [provider.entry, ordered ? `position ${index + 1} of ${count}` : '', provider.usable ? '' : `unavailable: ${provider.reason ?? 'not usable'}`]
    .filter(Boolean).join(', ');
  const tooltip = [provider.entry, provider.model, provider.effort, provider.reason].filter(Boolean).join('\n');
  return (
    <>
      <button ref={(node) => { trigger.current = node; register(node); }} type="button" className={['provider-chip', provider.usable ? '' : 'is-unusable'].filter(Boolean).join(' ')}
        data-entry={provider.entry} data-role={role.name} tabIndex={tabIndex} aria-label={name} aria-haspopup="menu" aria-expanded={open}
        aria-disabled={blocked || undefined} data-tooltip={tooltip} draggable={ordered && !blocked}
        onClick={() => { if (!blocked) setOpen((value) => !value); }} onFocus={onFocus} onKeyDown={onKeyDown} onContextMenu={onMenu}
        onDragStart={drag.start} onDragOver={drag.over} onDrop={drag.drop} onDragEnd={drag.end}>
        {ordered ? <span className="chip-order" aria-hidden="true">{index + 1}</span> : null}
        {provider.usable ? null : <Icon name="alert" className="chip-alert" />}
        <span className="chip-name"><Highlight text={provider.entry} query={settings.query} /></span>
      </button>
      {open ? <Menu id={`chip-menu-${role.name}-${index}`} label={`${provider.entry} model and effort`} open entries={entries} trigger={trigger} anchored
        onClose={(focusTrigger) => { setOpen(false); if (focusTrigger) trigger.current?.focus(); }} /> : null}
    </>
  );
}
