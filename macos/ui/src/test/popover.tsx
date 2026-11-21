// A fake popover adapter with the native event semantics, plus fixture envelopes.
import { act, render } from '@testing-library/react';
import { StrictMode } from 'react';
import { vi } from 'vitest';
import nothing from '../../../../tests/fixtures/app/usage-nothing.json';
import popover from '../../../../tests/fixtures/app/usage-popover.json';
import states from '../../../../tests/fixtures/popover/states.json';
import type { Envelope, PopoverApi } from '../popover/api';
import { PopoverApp } from '../popover/PopoverApp';
import type { AccountCheck, LoginResult, UpdateStatus } from '../shared';

export const usage = popover;
export const NOW = popover.generated_at;
const fixtures: Record<string, unknown> = { 'usage-popover.json': popover, 'usage-nothing.json': nothing };
type State = { fixture?: string; empty?: boolean; reading?: boolean; error?: string; update_version?: string };

export function envelope(name: keyof typeof states | null, generation = 1): Envelope {
  const state: State = name ? states[name] : { fixture: 'usage-popover.json' };
  const data = state.fixture ? fixtures[state.fixture] : state.empty ? { ...popover, providers: [] } : null;
  return { usage: data ? structuredClone(data) : null, error: state.error ?? null, reading: !!state.reading,
    generation, update_version: state.update_version ?? null };
}

/** Rust's update status. With only a version, the update is available. */
export function updateStatus(version: string | null, more: Partial<UpdateStatus> = {}): UpdateStatus {
  return { dialog: null, received: 0, total: null, version, phase: version ? 'available' : 'idle', retryable: false, detail: '', ...more };
}

export function fakeApi(first: Envelope, firstUpdate: UpdateStatus = updateStatus(first.update_version)) {
  const usageHandlers: ((value: Envelope) => void)[] = [];
  const updateHandlers: ((value: UpdateStatus) => void)[] = [];
  const statusHandlers: ((value: Envelope) => void)[] = [];
  const checkHandlers: ((value: AccountCheck[]) => void)[] = [];
  const unlistened: string[] = [];
  const done = () => Promise.resolve(undefined);
  const api = {
    getAccountChecks: vi.fn(() => Promise.resolve([] as AccountCheck[])),
    onAccountChecksStatus: vi.fn((handler: (checks: AccountCheck[]) => void) => {
      checkHandlers.push(handler);
      return Promise.resolve(() => { unlistened.push('account-checks-status'); checkHandlers.splice(checkHandlers.indexOf(handler), 1); });
    }),
    getSnapshot: vi.fn(() => Promise.resolve(first)),
    refreshUsage: vi.fn(done), hidePopover: vi.fn(done), openSettings: vi.fn(done),
    showPopoverMenu: vi.fn((_position: { x: number; y: number }): Promise<void> => done()),
    setPopoverHeight: vi.fn((_size: { height: number; durationMs: number; curve: number[] }) => done()),
    loginAccount: vi.fn((_runtime: string, _account: string): Promise<LoginResult> => Promise.resolve({ status: 'opened', copy_command: 'fake login command' })),
    getLoginCommand: vi.fn((_runtime: string, _account: string) => Promise.resolve('fake login command')),
    copyText: vi.fn((_text: string) => done()), checkAccount: vi.fn(done), quitApp: vi.fn(done),
    getUpdateStatus: vi.fn(() => Promise.resolve(structuredClone(firstUpdate))),
    onUpdateStatus: vi.fn((handler: (value: UpdateStatus) => void) => {
      updateHandlers.push(handler);
      return Promise.resolve(() => { unlistened.push('update-status'); updateHandlers.splice(updateHandlers.indexOf(handler), 1); });
    }),
    installUpdate: vi.fn((): Promise<void> => done()), openReleases: vi.fn(done),
    onUsageUpdated: vi.fn((handler: (value: Envelope) => void) => {
      usageHandlers.push(handler);
      return Promise.resolve(() => { unlistened.push('usage-updated'); usageHandlers.splice(usageHandlers.indexOf(handler), 1); });
    }),
    onPopoverStatus: vi.fn((handler: (value: Envelope) => void) => {
      statusHandlers.push(handler);
      return Promise.resolve(() => { unlistened.push('popover-status'); statusHandlers.splice(statusHandlers.indexOf(handler), 1); });
    }),
  } satisfies PopoverApi;
  return {
    api, unlistened,
    checks: (value: AccountCheck[]) => act(() => { checkHandlers.forEach((handler) => handler(value)); }),
    fresh: (value: Envelope) => act(() => { usageHandlers.forEach((handler) => handler(value)); }),
    status: (value: Envelope) => act(() => { statusHandlers.forEach((handler) => handler(value)); }),
    update: (value: UpdateStatus) => act(() => { updateHandlers.forEach((handler) => handler(structuredClone(value))); }),
  };
}

export async function renderPopover(first: Envelope = envelope(null), strict = false, firstUpdate?: UpdateStatus) {
  const fake = fakeApi(first, firstUpdate);
  const view = render(<PopoverApp api={fake.api} now={() => NOW} timeZone="UTC" />, { wrapper: strict ? StrictMode : undefined });
  await act(async () => { await Promise.resolve(); });
  await act(async () => { await Promise.resolve(); });
  return { ...fake, ...view };
}

/** The panel shows: body[data-active=true], as the native window focus does. */
export const show = () => act(() => { window.dispatchEvent(new Event('focus')); });
export const hide = () => act(() => { window.dispatchEvent(new Event('blur')); });
export const row = (key: string) => document.querySelector<HTMLLIElement>(`.account-row[data-key="${key}"]`);
