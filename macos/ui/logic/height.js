// Coalesce intrinsic-content observations, retaining snap requests until delivery.
// The adapter supplies CSS motion values and the native side owns all size caps.
export function createHeightReporter({ measure, send, schedule, reducedMotion, motion, onError }) {
  let pending = false, snap = true, shown = false, loading = true, lastHeight;
  function request() {
    if (pending) return;
    pending = true;
    schedule(() => {
      pending = false;
      const height = Math.ceil(measure());
      if (!Number.isFinite(height) || height <= 0) return;
      const mustSnap = snap || reducedMotion();
      if (height === lastHeight && !snap) return;
      const { durationMs, curve } = motion();
      snap = false; lastHeight = height;
      Promise.resolve(send({ height, durationMs: mustSnap ? 0 : durationMs, curve })).catch(onError);
    });
  }
  return {
    request,
    state(next) {
      if (loading && !next.loading) snap = true;
      loading = next.loading;
      request();
    },
    show() {
      if (!shown) { snap = true; shown = true; }
      request();
    },
  };
}

export function cssPanelMotion(styles) {
  const duration = styles.getPropertyValue('--panel-resize-time').trim();
  const curve = styles.getPropertyValue('--ease-out').match(/cubic-bezier\(([^)]+)\)/);
  return {
    durationMs: parseFloat(duration) * (duration.endsWith('ms') ? 1 : 1000),
    curve: curve ? curve[1].split(',').map(Number) : [],
  };
}

// Measure the intrinsic list, never its capped scrolling viewport. Absolute
// menus, tooltips and exiting rows do not contribute to the panel's height.
export function contentHeight(body, parts, style) {
  const number = (value) => parseFloat(value) || 0;
  const padding = style(body);
  return number(padding.paddingTop) + number(padding.paddingBottom) + parts.reduce((height, part) => {
    if (part.hidden) return height;
    const margins = style(part);
    return height + part.offsetHeight + number(margins.marginTop) + number(margins.marginBottom);
  }, 0);
}
