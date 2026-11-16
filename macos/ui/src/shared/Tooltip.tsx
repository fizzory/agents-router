// One tooltip per window. Any element with data-tooltip gets it on hover (after
// --tooltip-delay, then warm) or at once on keyboard focus. While shown, the
// tooltip adds its own id to the target's aria-describedby and later removes only
// that id, so a target's own descriptions survive.
import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { cssTime } from './motion';

interface TooltipApi { hide(): void }
const TooltipContext = createContext<TooltipApi>({ hide() {} });
export const useTooltip = () => useContext(TooltipContext);

const MARGIN = 6;

export function addToken(list: string | null, token: string): string {
  const tokens = (list ?? '').split(/\s+/).filter(Boolean);
  return tokens.includes(token) ? tokens.join(' ') : [...tokens, token].join(' ');
}

export function removeToken(list: string | null, token: string): string {
  return (list ?? '').split(/\s+/).filter((item) => item && item !== token).join(' ');
}

function describe(target: HTMLElement, id: string, on: boolean) {
  const next = on ? addToken(target.getAttribute('aria-describedby'), id) : removeToken(target.getAttribute('aria-describedby'), id);
  if (next) target.setAttribute('aria-describedby', next);
  else target.removeAttribute('aria-describedby');
}

export function TooltipProvider({ id = 'tooltip', children }: { id?: string; children: ReactNode }) {
  const node = useRef<HTMLDivElement>(null);
  const [tip, setTip] = useState<{ target: HTMLElement; text: string } | null>(null);
  const [visible, setVisible] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const warm = useRef(false);
  const current = useRef<HTMLElement | null>(null);

  const hide = useCallback(() => {
    clearTimeout(timer.current);
    current.current = null;
    setTip(null);
    setVisible(false);
  }, []);

  useEffect(() => {
    const show = (target: HTMLElement, immediate: boolean) => {
      clearTimeout(timer.current);
      current.current = target;
      const reveal = () => {
        if (current.current !== target || !target.isConnected) return;
        setTip({ target, text: target.dataset.tooltip ?? '' });
        warm.current = true;
      };
      if (immediate || warm.current) reveal();
      else timer.current = setTimeout(reveal, cssTime('--tooltip-delay'));
    };
    const closest = (event: Event) => (event.target instanceof Element ? event.target.closest<HTMLElement>('[data-tooltip]') : null);
    const over = (event: PointerEvent) => {
      const target = closest(event);
      if (target && target !== current.current) show(target, false);
      else if (!target) { hide(); warm.current = false; }
    };
    const focusIn = (event: FocusEvent) => {
      const target = closest(event);
      if (target) show(target, true);
      else hide();
    };
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { hide(); warm.current = false; }
    };
    document.addEventListener('pointerover', over);
    document.addEventListener('focusin', focusIn);
    document.addEventListener('focusout', hide);
    document.addEventListener('scroll', hide, true);
    document.addEventListener('pointerdown', hide);
    document.addEventListener('keydown', key);
    return () => {
      clearTimeout(timer.current);
      document.removeEventListener('pointerover', over);
      document.removeEventListener('focusin', focusIn);
      document.removeEventListener('focusout', hide);
      document.removeEventListener('scroll', hide, true);
      document.removeEventListener('pointerdown', hide);
      document.removeEventListener('keydown', key);
    };
  }, [hide]);

  // Position through CSSOM after the text is in, then fade in on the next frame.
  useLayoutEffect(() => {
    const element = node.current;
    if (!tip || !element) return;
    describe(tip.target, id, true);
    const box = tip.target.getBoundingClientRect();
    const size = element.getBoundingClientRect();
    const left = Math.max(MARGIN, Math.min(box.left, innerWidth - size.width - MARGIN));
    const top = box.top >= size.height + MARGIN ? box.top - size.height - 5 : Math.min(box.bottom + 5, innerHeight - size.height - MARGIN);
    element.style.setProperty('left', `${left}px`);
    element.style.setProperty('top', `${top}px`);
    const frame = requestAnimationFrame(() => setVisible(true));
    return () => {
      cancelAnimationFrame(frame);
      describe(tip.target, id, false);
    };
  }, [tip, id]);

  const api = useMemo(() => ({ hide }), [hide]);
  return (
    <TooltipContext.Provider value={api}>
      {children}
      <div ref={node} id={id} className={visible ? 'tooltip visible' : 'tooltip'} role="tooltip" hidden={!tip}>{tip?.text}</div>
    </TooltipContext.Provider>
  );
}
