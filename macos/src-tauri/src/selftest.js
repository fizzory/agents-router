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
        await wait(() => {
          result.progress.actual = document.querySelectorAll('.setting-row').length;
          return result.progress.actual === expected.general;
        });
        result.fixture = { count: document.querySelectorAll('.setting-row').length, ok: !!document.querySelector('[data-key="max_jobs"]') };
        for (const [pane, selector, count, texts] of [
          ['general', '#settings .setting-row:not([hidden])', expected.generalVisible, ['Max jobs']],
          ['roles', '#pane-content .role-row', expected.roles.length, expected.roles],
          ['accounts', '#pane-content .settings-account', expected.accounts, ['work']],
          ['sharing', '#pane-content .share-item', expected.sharing.length, expected.sharing],
          ['activity', '#pane-content .activity-row', expected.activity, ['Example invalid setting']],
        ]) {
          result.progress = { stage: pane, selector, expected: count };
          document.querySelector(`nav button[data-pane="${pane}"]`).click();
          await wait(() => {
            result.progress.actual = document.querySelectorAll(selector).length;
            return result.progress.actual === count;
          });
          const host = document.querySelector(pane === 'general' ? '#settings' : '#pane-content');
          await sleep();
          const box = layout(host), nodes = [...document.querySelectorAll(selector)];
          result.panes[pane] = { ...box, count: nodes.length, fixture: texts.every(text => host.textContent.toLowerCase().includes(text.toLowerCase())), rows: nodes.every(node => layout(node).ok) };
        }
      }
      result.ready = true;
    } catch (cause) { error('probe', cause.stack || cause); }
  }, { once: true });
})();
