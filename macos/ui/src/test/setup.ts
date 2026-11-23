// jsdom lacks matchMedia, ResizeObserver and computed custom properties.
// Tests get stubs, and code reads the real motion.css token values.
// dom first: React DOM picks its animationend event name when it loads.
import { media, resizeObservers } from './dom';
import { cleanup } from '@testing-library/react';
import { afterEach, beforeEach } from 'vitest';
import motion from '../../motion.css?raw';

const tokens = new Map([...motion.matchAll(/(--[\w-]+)\s*:\s*([^;}]+)/g)].map((match) => [match[1] ?? '', (match[2] ?? '').trim()]));

// jsdom computes no custom properties, so motion.css tokens (declared on :root,
// inherited everywhere) are answered from the file itself.
const computed = window.getComputedStyle.bind(window);
const withTokens = ((element: Element, pseudo?: string | null) => {
  const style = computed(element, pseudo);
  return new Proxy(style, {
    get(target, key) {
      if (key === 'getPropertyValue') {
        return (name: string) => (name.startsWith('--') ? tokens.get(name) ?? '' : target.getPropertyValue(name));
      }
      const value: unknown = Reflect.get(target, key, target);
      return typeof value === 'function' ? value.bind(target) : value;
    },
  });
}) as typeof window.getComputedStyle;
window.getComputedStyle = withTokens;
globalThis.getComputedStyle = withTokens;

beforeEach(() => {
  window.localStorage.clear();
  media.reset();
  resizeObservers.length = 0;
  document.body.dataset.active = 'false';
});

afterEach(() => cleanup());
