import { describe, expect, it, vi } from 'vitest';
import { SettingsReads } from './reads';

/** A read whose every call waits until the test settles it. */
function heldRead() {
  const calls: { resolve: () => void; reject: (error: unknown) => void }[] = [];
  const read = vi.fn(() => new Promise<void>((resolve, reject) => { calls.push({ resolve, reject }); }));
  return { read, calls };
}
const flush = async () => { for (let round = 0; round < 6; round += 1) await Promise.resolve(); };

describe('SettingsReads', () => {
  it('a read that rejects still runs the pass reserved behind it, then reports that pass’s error', async () => {
    const reads = new SettingsReads(() => Promise.resolve(true), () => false);
    const { read, calls } = heldRead();
    const first = reads.request('activity', read);
    await flush();
    expect(read).toHaveBeenCalledTimes(1);
    const second = reads.request('activity', read);
    calls[0]!.reject(new Error('agt failed'));
    await flush();
    expect(read).toHaveBeenCalledTimes(2);
    calls[1]!.reject(new Error('agt failed again'));
    await expect(first).rejects.toThrow('agt failed again');
    await expect(second).rejects.toThrow('agt failed again');
  });

  it('a pass that succeeds after a rejected one settles the request without an error', async () => {
    const reads = new SettingsReads(() => Promise.resolve(true), () => false);
    const { read, calls } = heldRead();
    const done = reads.request('activity', read);
    await flush();
    reads.request('activity', read).catch(() => {});
    calls[0]!.reject(new Error('agt failed'));
    await flush();
    expect(read).toHaveBeenCalledTimes(2);
    calls[1]!.resolve();
    await expect(done).resolves.toBeUndefined();
    expect(reads.dirty.has('activity')).toBe(false);
  });
});
