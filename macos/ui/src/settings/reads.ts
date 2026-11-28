// Requests for one workspace, pane or share status share a queued read: one
// running, and one more pass reserved behind it by a forced request.
//
// A pass an event asked for (automatic) starts only while Settings is on screen
// and no write holds the guard. So does every pass behind another one, whoever
// asked. A person's own action starts its first pass at once. Hidden, the key is
// marked dirty for the next open or selection. Behind a write, the pass is kept
// and asked for again when the write releases. A read that rejects still lets
// the pass behind it run; the request then rejects with the last pass's error,
// for the caller's own report, unless a later pass succeeded.
export interface ReadOptions { automatic?: boolean }
type Read = (options: ReadOptions) => Promise<void>;
interface Slot {
  pending?: ReadOptions;
  done: Promise<void>;
}
const waiting = (slot: Slot): ReadOptions | undefined => slot.pending;

export class SettingsReads {
  private slots = new Map<string, Slot>();
  private deferred = new Map<string, Read>();
  private live = true;
  private epoch = 0;
  readonly dirty = new Set<string>();

  constructor(private visible: () => Promise<boolean>, private writing: () => boolean) {}

  request(key: string, read: Read, options: ReadOptions = {}, force = true): Promise<void> {
    if (!this.live) return Promise.resolve();
    const active = this.slots.get(key);
    if (active) {
      if (active.pending) active.pending = { automatic: active.pending.automatic && options.automatic };
      else if (force) active.pending = options;
      return active.done;
    }

    const slot: Slot = { pending: options, done: Promise.resolve() };
    this.slots.set(key, slot);
    slot.done = Promise.resolve().then(() => this.run(key, slot, read)).finally(() => {
      if (this.slots.get(key) === slot) this.slots.delete(key);
    });
    return slot.done;
  }

  /** A write released the guard: ask again for what waited behind it. */
  resume(): void {
    const kept = [...this.deferred];
    this.deferred.clear();
    for (const [key, read] of kept) this.request(key, read, { automatic: true }).catch(() => {});
  }

  /** The window mounted (again, under StrictMode). */
  start(): void {
    this.live = true;
  }

  /** The window unmounted: no pass starts from here on, kept or reserved. */
  dispose(): void {
    this.live = false;
    this.epoch += 1;
    this.deferred.clear();
    for (const slot of this.slots.values()) slot.pending = undefined;
    this.slots.clear();
  }

  private defer(key: string, read: Read): void {
    this.deferred.set(key, read);
    this.dirty.add(key);
  }

  private async run(key: string, slot: Slot, read: Read): Promise<void> {
    const epoch = this.epoch;
    const stopped = () => !this.live || this.epoch !== epoch;
    let trailing = false;
    let failed: { error: unknown } | undefined;
    while (slot.pending && !stopped()) {
      const next = slot.pending;
      slot.pending = undefined;
      if (next.automatic && this.writing()) {
        this.defer(key, read);
        continue;
      }
      if (next.automatic || trailing) {
        const visible = await this.visible().catch(() => false);
        if (stopped()) return;
        const later = waiting(slot);
        if (later) {
          // Asked for during the check: decide again, now, for both.
          slot.pending = { automatic: next.automatic && later.automatic };
          continue;
        }
        if (!visible) {
          this.dirty.add(key);
          continue;
        }
        if (next.automatic && this.writing()) {
          this.defer(key, read);
          continue;
        }
      }
      this.dirty.delete(key);
      this.deferred.delete(key);
      trailing = true;
      try {
        await read(next);
        failed = undefined;
      } catch (error) {
        failed = { error };
      }
    }
    if (failed && !stopped()) throw failed.error;
  }
}
