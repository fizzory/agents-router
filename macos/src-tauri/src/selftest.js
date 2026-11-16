// Compiled into the ci-selftest Rust module only, never into frontendDist.
;(() => {
  const result = window.__AGT_CI_SELFTEST = { ready: false, errors: [], probes: {}, panes: {} };
  const error = (kind, message) => result.errors.push({ kind, message: String(message) });
  addEventListener('error', event => error(event.target === window ? 'script' : 'resource', event.message || event.target.src || event.target.href), true);
  addEventListener('unhandledrejection', event => error('rejection', event.reason));
  addEventListener('securitypolicyviolation', event => error('csp', `${event.violatedDirective}: ${event.blockedURI}`));
  addEventListener('pagehide', () => error('reload', 'page hidden or navigated'));
  const sleep = () => new Promise(resolve => setTimeout(resolve, 40));
  const wait = async condition => { while (!condition()) await sleep(); };
  const layout = node => {
    const { width, height } = node.getBoundingClientRect();
    return { width, height, ok: width > 0 && height > 0 };
  };
  addEventListener('load', async () => {
    try {
      result.probes = {
        'css-has': CSS.supports('selector(:has(*))'),
        'css-color-mix': CSS.supports('color', 'color-mix(in srgb, red, blue)'),
        'css-container': CSS.supports('container-type', 'inline-size'),
        'css-custom-property': getComputedStyle(document.documentElement).getPropertyValue('--panel-time').trim().length > 0,
        'css-grid': CSS.supports('display', 'grid'),
        'js-es-modules': 'noModule' in document.createElement('script'),
        'js-resize-observer': typeof ResizeObserver === 'function',
        'js-structured-clone': structuredClone({ value: 7 }).value === 7,
        'js-array-at': [3, 7].at(-1) === 7,
        'js-queue-microtask': await new Promise(resolve => queueMicrotask(() => resolve(true))),
      };
      const expected = __AGT_EXPECTED__;
      result.progress = { stage: 'fixture', expected };
      if (location.pathname.endsWith('popover.html')) {
        await wait(() => document.querySelectorAll('#accounts .account-row').length === expected.popover.length);
        const labels = [...document.querySelectorAll('#accounts .row-link')].map(node => node.getAttribute('aria-label'));
        result.fixture = { count: labels.length, names: expected.popover, ok: expected.popover.every(name => labels.some(label => label.includes(name))) };
        result.layout = layout(document.querySelector('#accounts'));
      } else {
        // The retained webview may load before Rust opens Settings at General.
        await wait(() => window.__AGT_CI_BEGIN === true);
        const keyNodes = () => [...document.querySelectorAll('#pane-content [data-setting-key]')];
        const keyOf = node => node.getAttribute('data-setting-key');
        const sameKeys = (keys, want) => {
          const wanted = new Set(want);
          return keys.length === want.length && new Set(keys).size === keys.length && keys.every(key => wanted.has(key));
        };
        const providers = ['claude', 'codex', 'antigravity', 'kiro'];
        const seen = [];
        for (const [pane, selector, count, texts] of [
          ['general', '[data-setting-key]', expected.keys.general.length, ['Max jobs']],
          ['roles', '.role-row', expected.roles.length, expected.roles],
          ['accounts', '.settings-account', expected.accounts, ['work']],
          ['sharing', '.share-item', expected.sharing.length, expected.sharing],
          ['activity', '.activity-row', expected.activity, ['Example invalid setting']],
          ...providers.map(provider => [provider, '[data-setting-key]', expected.keys[provider].length, ['Max jobs']]),
        ]) {
          const want = expected.keys[pane], button = `nav button[data-pane="${pane}"]`;
          result.progress = { stage: pane, selector, expected: count, keys: want.length };
          await wait(() => document.querySelector(button));
          document.querySelector(button).click();
          await wait(() => {
            const nav = document.querySelector(button), keys = keyNodes().map(keyOf);
            Object.assign(result.progress, {
              pane: document.body.dataset.pane, current: nav && nav.getAttribute('aria-current'),
              actual: document.querySelectorAll(`#pane-content ${selector}`).length, actualKeys: keys.length,
            });
            return result.progress.pane === pane && result.progress.current === 'page'
              && sameKeys(keys, want) && result.progress.actual === count;
          });
          // File-only rows sit in closed <details> with zero size until opened.
          for (const details of document.querySelectorAll('#pane-content details')) details.open = true;
          await sleep();
          const host = document.querySelector('#pane-content'), box = layout(host);
          const keyed = keyNodes(), nodes = [...document.querySelectorAll(`#pane-content ${selector}`)];
          seen.push(...keyed.map(keyOf));
          result.panes[pane] = {
            ...box, count: nodes.length, keys: keyed.length,
            fixture: texts.every(text => host.textContent.toLowerCase().includes(text.toLowerCase())),
            rows: [...new Set([...keyed, ...nodes])].every(node => layout(node).ok),
          };
        }
        const all = Object.values(expected.keys).flat();
        result.fixture = { count: seen.length, ok: sameKeys(seen, all) };
      }
      result.ready = true;
    } catch (cause) { error('probe', cause.stack || cause); }
  }, { once: true });
})();
