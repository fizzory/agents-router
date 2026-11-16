// A fake popover adapter with the native event semantics, plus fixture envelopes.
import { act, render } from '@testing-library/react';
import { vi } from 'vitest';
import nothing from '../../../../tests/fixtures/app/usage-nothing.json';
import popover from '../../../../tests/fixtures/app/usage-popover.json';
import states from '../../../../tests/fixtures/popover/states.json';
import type { Envelope, PopoverApi } from '../popover/api';
import { PopoverApp } from '../popover/PopoverApp';

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

export function fakeApi(first: Envelope) {
  const usageHandlers: ((value: Envelope) => void)[] = [];
  const statusHandlers: ((value: Envelope) => void)[] = [];
  const unlistened: string[] = [];
  const done = () => Promise.resolve(undefined);
  const api = {
    getSnapshot: vi.fn(() => Promise.resolve(first)),
    refreshUsage: vi.fn(done), hidePopover: vi.fn(done), openSettings: vi.fn(done),
    setPopoverHeight: vi.fn((_size: { height: number; durationMs: number; curve: number[] }) => done()),
    loginAccount: vi.fn(done), checkAccount: vi.fn(done), checkForUpdates: vi.fn(done), quitApp: vi.fn(done),
    onUsageUpdated: vi.fn((handler: (value: Envelope) => void) => {
      usageHandlers.push(handler);
      return Promise.resolve(() => { unlistened.push('usage-updated'); });
    }),
    onPopoverStatus: vi.fn((handler: (value: Envelope) => void) => {
      statusHandlers.push(handler);
      return Promise.resolve(() => { unlistened.push('popover-status'); });
    }),
  } satisfies PopoverApi;
  return {
    api, unlistened,
    fresh: (value: Envelope) => act(() => { usageHandlers.forEach((handler) => handler(value)); }),
    status: (value: Envelope) => act(() => { statusHandlers.forEach((handler) => handler(value)); }),
  };
}

export async function renderPopover(first: Envelope = envelope(null)) {
  const fake = fakeApi(first);
  const view = render(<PopoverApp api={fake.api} now={() => NOW} timeZone="UTC" />);
  await act(async () => { await Promise.resolve(); });
  await act(async () => { await Promise.resolve(); });
  return { ...fake, ...view };
}

/** The panel shows: body[data-active=true], as the native window focus does. */
export const show = () => act(() => { window.dispatchEvent(new Event('focus')); });
export const hide = () => act(() => { window.dispatchEvent(new Event('blur')); });
export const row = (key: string) => document.querySelector<HTMLLIElement>(`.account-row[data-key="${key}"]`);
