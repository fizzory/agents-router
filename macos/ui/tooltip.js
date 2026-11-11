// One tooltip component for both retained windows. All visual timing is CSS.
export function attachTooltips(tooltip) {
  let timer, target = null, warm = false;
  const hide = () => {
    clearTimeout(timer);
    target?.removeAttribute('aria-describedby');
    target = null; tooltip.hidden = true; tooltip.classList.remove('visible');
  };
  const show = (next, immediate) => {
    hide(); target = next;
    const reveal = () => {
      if (!next.isConnected) return;
      tooltip.textContent = next.dataset.tooltip; tooltip.hidden = false;
      const box = next.getBoundingClientRect(), size = tooltip.getBoundingClientRect();
      tooltip.style.left = `${Math.max(6, Math.min(box.left, innerWidth - size.width - 6))}px`;
      tooltip.style.top = `${box.top >= size.height + 6 ? box.top - size.height - 5 : Math.min(box.bottom + 5, innerHeight - size.height - 6)}px`;
      next.setAttribute('aria-describedby', tooltip.id);
      requestAnimationFrame(() => tooltip.classList.add('visible')); warm = true;
    };
    const delay = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--tooltip-delay'));
    if (immediate || warm) reveal(); else timer = setTimeout(reveal, delay);
  };
  document.addEventListener('pointerover', event => {
    const next = event.target.closest('[data-tooltip]');
    if (next && next !== target) show(next, false);
    else if (!next) { hide(); warm = false; }
  });
  document.addEventListener('focusin', event => { const next = event.target.closest('[data-tooltip]'); if (next) show(next, true); else hide(); });
  document.addEventListener('focusout', hide);
  document.addEventListener('scroll', hide, true);
  document.addEventListener('pointerdown', hide);
  document.addEventListener('keydown', event => { if (event.key === 'Escape') { hide(); warm = false; } });
  return hide;
}
