// The three sheets. Installer copy is frontend-owned (logic/install.js) with
// Rust's message and detail verbatim; the updater shows Rust's dialog verbatim.
import { useEffect, useRef, useState } from 'react';
import { AccountNameCheck } from '../../logic/panes.js';
import { dialogKey, installDialog } from '../../logic/install.js';
import { PopupButton, ProgressBar, Sheet, type MenuEntry } from '../shared';
import type { InstallStatus, SettingsApi, UpdateStatus } from './api';
import { errorMessage, PROVIDER_NAMES } from './model';

interface InstallModel { title: string; detail: string; buttons: [string, string][]; default?: string; cancel?: string; busy?: boolean }

export function InstallerSheet({ status, api, reread }: { status: InstallStatus; api: SettingsApi; reread: () => Promise<void> }) {
  const model = installDialog(status) as InstallModel | null;
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const key = JSON.stringify(model);
  useEffect(() => { setMessage(null); }, [key]);
  if (!model) return null;
  const run = async (action: string) => {
    if (pending) return;
    setPending(true);
    setMessage(null);
    try { await api.installAction(action); await reread(); }
    catch (error) { await reread(); setMessage(errorMessage(error)); }
    finally { setPending(false); }
  };
  return (
    <Sheet name="install" title={model.title} detail={model.detail || undefined} busy={!!model.busy} pending={pending}
      buttons={model.buttons.map(([label, action]) => ({ label, action }))} defaultAction={dialogKey(model, 'Enter') ?? null}
      cancelAction={dialogKey(model, 'Escape') ?? null} onAction={run} message={message ? { tone: 'error', text: message } : null}>
      {model.busy ? <ProgressBar label={model.title} valueText={model.title} /> : null}
    </Sheet>
  );
}

const MB = 1024 * 1024;
const megabytes = (bytes: number) => (bytes / MB).toFixed(1);

export function UpdaterSheet({ status, api, reread }: { status: UpdateStatus; api: SettingsApi; reread: () => Promise<void> }) {
  const dialog = status.dialog;
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  useEffect(() => { setMessage(null); setPending(false); }, [dialog?.title]);
  if (!dialog) return null;
  const run = async (action: string) => {
    if (pending) return;
    setPending(true);
    setMessage(null);
    try { await api.updateAction(action); }
    catch (error) { await reread(); setMessage(errorMessage(error)); }
    finally { setPending(false); }
  };
  const close = dialog.buttons.some(([, action]) => action === 'close') ? 'close' : null;
  const total = status.total ?? null;
  const received = status.received ?? 0;
  return (
    <Sheet name="update" title={dialog.title} detail={dialog.detail || undefined} busy={dialog.busy} pending={pending}
      buttons={dialog.buttons.map(([label, action]) => ({ label, action }))} defaultAction={dialog.busy ? null : dialog.default}
      cancelAction={close} onAction={run} message={message ? { tone: 'error', text: message } : null}>
      {dialog.busy ? <ProgressBar label={dialog.title} value={total ? received : null} max={total}
        valueText={total ? `${megabytes(received)} of ${megabytes(total)} MB` : dialog.title} /> : null}
    </Sheet>
  );
}

type NameState = { valid: boolean; checking: boolean; message: string } | null;

export function AddAccountSheet({ api, onClose }: { api: SettingsApi; onClose: (added: boolean) => void }) {
  const [runtime, setRuntime] = useState('claude');
  const [name, setName] = useState('');
  const [check, setCheck] = useState<NameState>(null);
  const [submitting, setSubmitting] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const checker = useRef<{ validate(runtime: string, name: string): Promise<NameState>; invalidate(): void } | null>(null);
  checker.current ??= new AccountNameCheck(api.accountsAddCheck) as unknown as { validate(runtime: string, name: string): Promise<NameState>; invalidate(): void };
  const input = useRef<HTMLInputElement>(null);
  const closed = useRef(false);
  // Reset on setup too: StrictMode replays effects, and a stale true would drop every name check.
  useEffect(() => {
    closed.current = false;
    return () => { closed.current = true; checker.current?.invalidate(); };
  }, []);

  const validate = async (nextRuntime: string, nextName: string) => {
    setFailure(null);
    setCheck(nextName.trim() ? { valid: false, checking: true, message: '' } : null);
    const state = await checker.current?.validate(nextRuntime, nextName);
    if (state && !closed.current) setCheck(state);
  };
  const valid = !!check?.valid && !submitting;
  const submit = async () => {
    if (!valid) return;
    setSubmitting(true);
    try {
      await api.accountsAdd(runtime, name);
      closed.current = true;
      onClose(true);
    } catch (error) {
      setSubmitting(false);
      setFailure(errorMessage(error));
      input.current?.focus();
      // The name check runs again by itself; no edit is needed to retry.
      const state = await checker.current?.validate(runtime, name);
      if (state && !closed.current) setCheck(state);
    }
  };
  const act = (action: string) => {
    if (action === 'cancel') { if (!submitting) { closed.current = true; onClose(false); } }
    else submit();
  };
  const entries: MenuEntry[] = Object.entries(PROVIDER_NAMES).filter(([id]) => id !== 'kiro').map(([id, label]) => ({
    kind: 'radio', label, checked: runtime === id, onSelect: () => { setRuntime(id); validate(id, name); },
  }));
  const line = submitting ? 'Opening Terminal…' : failure ?? (check?.checking ? 'Checking name…' : check?.message || '');
  const invalid = !!failure || (!!check && !check.valid && !check.checking && !!check.message);
  return (
    <Sheet name="add-account" title="Add account" buttons={[{ label: 'Cancel', action: 'cancel', disabled: submitting },
      { label: 'Add account…', action: 'add', disabled: !valid }]} defaultAction="add" cancelAction="cancel" onAction={act}
      pending={submitting} canSubmit={valid} initialFocus={() => input.current}>
      <div className="sheet-field">
        <span className="sheet-label" id="add-account-provider-label">Provider</span>
        <PopupButton id="add-account-provider" label="Provider" ariaLabel={`Provider, ${PROVIDER_NAMES[runtime]}`} text={PROVIDER_NAMES[runtime]}
          entries={entries} disabled={submitting} />
      </div>
      <div className="sheet-field">
        <label className="sheet-label" htmlFor="new-account-name">Name</label>
        <input ref={input} id="new-account-name" className="text-field" type="text" autoComplete="off" spellCheck={false} autoCapitalize="off"
          value={name} readOnly={submitting} aria-describedby="account-name-message" aria-invalid={invalid || undefined}
          onChange={(event) => { const next = event.currentTarget.value; setName(next); validate(runtime, next); }} />
      </div>
      <p id="account-name-message" className={invalid ? 'sheet-message is-error' : 'sheet-message'} role="status">{line}</p>
    </Sheet>
  );
}
