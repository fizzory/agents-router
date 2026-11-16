// The Settings frame: sidebar navigation, search field and the scope popup.
import type { Ref } from 'react';
import { Icon } from './Icon';
import type { MenuEntry } from './Menu';
import { PopupButton } from './PopupButton';

export interface NavItem { id: string; label: string; attention?: boolean; count?: number | null; divider?: boolean }

export function SidebarNav({ items, selected, onSelect }: { items: NavItem[]; selected: string; onSelect: (id: string) => void }) {
  return (
    <nav className="sidebar-nav" aria-label="Settings panes">
      {items.map((item) => {
        const searching = item.count != null && item.id !== selected;
        const name = [item.label, item.attention ? 'needs attention' : '',
          searching ? (item.count ? `${item.count} result${item.count === 1 ? '' : 's'}` : 'no results') : ''].filter(Boolean).join(', ');
        const classes = ['nav-item', searching && !item.count ? 'is-empty' : ''].filter(Boolean).join(' ');
        return (
          <div key={item.id} className="nav-entry">
            {item.divider ? <hr className="nav-divider" /> : null}
            <button type="button" className={classes} data-pane={item.id} aria-current={item.id === selected ? 'page' : undefined}
              aria-label={name} onClick={() => onSelect(item.id)}>
              <span className="nav-label">{item.label}</span>
              {searching && item.count ? <span className="nav-count" aria-hidden="true">{item.count}</span> : null}
              {item.attention ? <span className="attention-dot" aria-hidden="true" /> : null}
            </button>
          </div>
        );
      })}
    </nav>
  );
}

export interface SearchFieldProps { value: string; onChange: (value: string) => void; inputRef?: Ref<HTMLInputElement> }

export function SearchField({ value, onChange, inputRef }: SearchFieldProps) {
  return (
    <label className="search-field">
      <Icon name="search" />
      <input ref={inputRef} id="search" type="search" placeholder="Search" aria-label="Search settings" autoComplete="off" spellCheck={false}
        value={value} onChange={(event) => onChange(event.currentTarget.value)}
        onKeyDown={(event) => {
          if (event.key !== 'Escape') return;
          event.preventDefault();
          onChange('');
          event.currentTarget.blur();
        }} />
    </label>
  );
}

export interface Scope { level: 'user' | 'workspace' | 'local'; workspace: string | null }
export const folderName = (path: string) => path.split('/').filter(Boolean).pop() ?? path;

export function scopeText(scope: Scope) {
  if (scope.level === 'user' || !scope.workspace) return 'User';
  return `${folderName(scope.workspace)}: ${scope.level === 'local' ? 'Local' : 'Workspace'}`;
}

export function ScopePopup({ scope, folders, onSelect, onChoose }: { scope: Scope; folders: string[]; onSelect: (scope: Scope) => void; onChoose: () => void }) {
  const entries: MenuEntry[] = [
    { kind: 'radio', label: 'User', checked: scope.level === 'user', onSelect: () => onSelect({ level: 'user', workspace: null }) },
  ];
  for (const folder of folders) {
    entries.push({ kind: 'heading', label: folderName(folder) });
    for (const level of ['workspace', 'local'] as const) {
      entries.push({ kind: 'radio', label: level === 'local' ? 'Local' : 'Workspace', checked: scope.workspace === folder && scope.level === level,
        onSelect: () => onSelect({ level, workspace: folder }) });
    }
  }
  entries.push({ kind: 'separator' }, { kind: 'item', label: 'Choose Folder…', onSelect: onChoose });
  return (
    <PopupButton id="scope" className="scope-button" label="Settings scope" ariaLabel={`Settings scope, ${scopeText(scope)}`}
      text={scopeText(scope)} tooltip={scope.level === 'user' || !scope.workspace ? 'User settings' : scope.workspace} entries={entries} />
  );
}
