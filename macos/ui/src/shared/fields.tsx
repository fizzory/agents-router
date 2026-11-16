// Number and text fields. A field owns its draft while focused: data refreshes
// don't overwrite it. Enter or leaving the field commits once; Escape restores.
import { useRef, useState, type KeyboardEvent } from 'react';
import { Icon } from './Icon';

interface FieldBase {
  id: string;
  describedBy?: string;
  /** The value is inherited or a default: shown in muted ink. */
  inherited?: boolean;
  disabled?: boolean;
  disabledReason?: string;
  /** Another write is running: input is ignored, nothing dims. */
  locked?: boolean;
  invalid?: boolean;
  errorId?: string;
  onCommit: (text: string) => void;
  /** Called when a draft starts (focus), so the owner can keep the revision the draft was typed against. */
  onDraftStart?: () => void;
  /** Called when the draft ends (blur, after any commit), so a kept revision never outlives it. */
  onDraftEnd?: () => void;
}

function useDraft(display: string, readOnly: boolean, onCommit: (text: string) => void, prepare: (text: string) => string = (text) => text,
  onDraftStart?: () => void, onDraftEnd?: () => void) {
  const [draft, setDraft] = useState<string | null>(null);
  const baseline = useRef(display);
  const skip = useRef(false);
  const commit = (text: string) => {
    setDraft(null);
    if (readOnly) return;
    const next = prepare(text);
    if (next === baseline.current) return;
    baseline.current = next;
    onCommit(next);
  };
  return {
    value: draft ?? display,
    draft,
    setDraft,
    commit,
    onFocus: () => { baseline.current = display; skip.current = false; setDraft(display); onDraftStart?.(); },
    onBlur: (text: string) => {
      if (skip.current) { skip.current = false; setDraft(null); }
      else commit(text);
      onDraftEnd?.();
    },
    onKeyDown: (event: KeyboardEvent<HTMLInputElement>) => {
      if (event.key === 'Enter') { event.preventDefault(); event.currentTarget.blur(); }
      else if (event.key === 'Escape') { event.preventDefault(); skip.current = true; setDraft(null); event.currentTarget.blur(); }
    },
  };
}

const fieldClass = (base: string, inherited?: boolean, invalid?: boolean, extra?: string) =>
  [base, inherited ? 'is-inherited' : '', invalid ? 'is-invalid' : '', extra ?? ''].filter(Boolean).join(' ');

export interface NumberFieldProps extends FieldBase {
  value: number | null;
  kind: 'int' | 'float';
  minimum?: number | null;
  /** Read out after the value, for example "User". */
  source?: string;
  label: string;
}

export function NumberField({ id, value, kind, minimum, source, label, describedBy, inherited, disabled, disabledReason, locked, invalid, errorId, onCommit, onDraftStart, onDraftEnd }: NumberFieldProps) {
  const display = value == null ? '' : String(value);
  const readOnly = !!(disabled || disabledReason || locked);
  const step = kind === 'float' ? 0.05 : 1;
  const clamp = (text: string) => {
    const number = Number(text);
    if (text.trim() === '' || !Number.isFinite(number)) return text.trim();
    return String(minimum != null ? Math.max(minimum, number) : number);
  };
  const field = useDraft(display, readOnly, (text) => { if (text !== '') onCommit(text); }, clamp, onDraftStart, onDraftEnd);
  const stepped = (direction: number) => {
    const base = Number(field.value || minimum || 0);
    const next = Math.max(minimum ?? -Infinity, (Number.isFinite(base) ? base : 0) + direction * step);
    return String(Number(next.toFixed(4)));
  };
  const keyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
      event.preventDefault();
      if (!readOnly) field.setDraft(stepped(event.key === 'ArrowUp' ? 1 : -1));
      return;
    }
    field.onKeyDown(event);
  };
  const click = (direction: number) => {
    if (readOnly) return;
    const next = stepped(direction);
    if (field.draft !== null) field.setDraft(next);
    else field.commit(next);
  };
  return (
    <span className={fieldClass('number-field', inherited, invalid)} data-tooltip={disabledReason}>
      <input id={id} type="text" role="spinbutton" inputMode={kind === 'int' ? 'numeric' : 'decimal'} autoComplete="off" spellCheck={false}
        value={field.value} placeholder="Not set" readOnly={readOnly} aria-describedby={describedBy}
        aria-valuemin={minimum ?? undefined} aria-valuenow={value ?? undefined}
        aria-valuetext={`${field.value || 'Not set'}${source ? `, ${source}` : ''}`}
        aria-invalid={invalid || undefined} aria-errormessage={invalid ? errorId : undefined}
        onFocus={field.onFocus} onBlur={(event) => field.onBlur(event.currentTarget.value)}
        onChange={(event) => field.setDraft(event.currentTarget.value)} onKeyDown={keyDown} />
      {/* − and + side by side: each gets a 24 × 24 hit target inside the 32px row. */}
      <span className="stepper" aria-hidden="true">
        <button type="button" tabIndex={-1} aria-label={`Decrease ${label}`} disabled={readOnly}
          onMouseDown={(event) => event.preventDefault()} onClick={() => click(-1)}><Icon name="minus" /></button>
        <button type="button" tabIndex={-1} aria-label={`Increase ${label}`} disabled={readOnly}
          onMouseDown={(event) => event.preventDefault()} onClick={() => click(1)}><Icon name="plus" /></button>
      </span>
    </span>
  );
}

export interface TextFieldProps extends FieldBase {
  value: string;
  /** names: a list shown with ", " between items; wider column. */
  variant?: 'text' | 'names';
  placeholder?: string;
}

export function TextField({ id, value, variant = 'text', placeholder = 'Not set', describedBy, inherited, disabled, disabledReason, locked, invalid, errorId, onCommit,
  onDraftStart, onDraftEnd }: TextFieldProps) {
  const readOnly = !!(disabled || disabledReason || locked);
  const field = useDraft(value, readOnly, onCommit, undefined, onDraftStart, onDraftEnd);
  return (
    <input id={id} type="text" className={fieldClass('text-field', inherited, invalid, variant === 'names' ? 'is-names' : '')}
      autoComplete="off" spellCheck={false} value={field.value} placeholder={placeholder} readOnly={readOnly}
      aria-describedby={describedBy} aria-invalid={invalid || undefined} aria-errormessage={invalid ? errorId : undefined}
      data-tooltip={disabledReason}
      onFocus={field.onFocus} onBlur={(event) => field.onBlur(event.currentTarget.value)}
      onChange={(event) => field.setDraft(event.currentTarget.value)} onKeyDown={field.onKeyDown} />
  );
}
