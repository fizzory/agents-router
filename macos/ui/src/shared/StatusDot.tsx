// Eight availability states. Colour is never the only signal: rows also carry a word or button.
export function StatusDot({ state }: { state: string }) {
  return <span className="status-dot" data-state={state} aria-hidden="true" />;
}
