// Eight availability states. Colour is never the only signal: rows also carry a word or button.
export function StatusDot({ state, shape, breathing = false }: { state: string; shape?: string; breathing?: boolean }) {
  return <span className="status-dot" data-state={state} data-shape={shape} data-breathing={breathing} aria-hidden="true" />;
}
