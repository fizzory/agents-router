// Small jsdom stand-ins: a controllable matchMedia and a recording ResizeObserver.
type Listener = (event: { matches: boolean }) => void;

export const media = {
  reduced: false,
  listeners: new Set<Listener>(),
  reset() { this.reduced = false; this.listeners.clear(); },
  setReduced(value: boolean) {
    this.reduced = value;
    for (const listener of this.listeners) listener({ matches: value });
  },
};

window.matchMedia = ((query: string) => ({
  get matches() { return query.includes('prefers-reduced-motion') && media.reduced; },
  media: query,
  onchange: null,
  addEventListener: (_type: string, listener: Listener) => media.listeners.add(listener),
  removeEventListener: (_type: string, listener: Listener) => media.listeners.delete(listener),
  addListener: (listener: Listener) => media.listeners.add(listener),
  removeListener: (listener: Listener) => media.listeners.delete(listener),
  dispatchEvent: () => true,
})) as unknown as typeof window.matchMedia;

export class FakeResizeObserver {
  observed = new Set<Element>();
  disconnected = false;
  constructor(public callback: ResizeObserverCallback) { resizeObservers.push(this); }
  observe(target: Element) { this.observed.add(target); }
  unobserve(target: Element) { this.observed.delete(target); }
  disconnect() { this.disconnected = true; this.observed.clear(); }
}
export const resizeObservers: FakeResizeObserver[] = [];
window.ResizeObserver = FakeResizeObserver as unknown as typeof ResizeObserver;

// jsdom has no AnimationEvent; without it React listens for webkitAnimationEnd.
if (!('AnimationEvent' in window)) {
  (window as { AnimationEvent?: unknown }).AnimationEvent = class AnimationEvent extends Event {
    animationName = '';
    elapsedTime = 0;
    pseudoElement = '';
  };
}
