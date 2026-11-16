// One polite live region per window (#announcement): "Copied", "Undid …", moves.
import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react';

const AnnounceContext = createContext<(text: string) => void>(() => {});
export const useAnnounce = () => useContext(AnnounceContext);

export function AnnounceProvider({ children }: { children: ReactNode }) {
  const [message, setMessage] = useState({ text: '', serial: 0 });
  // The same text twice still announces: a zero-width space alternates.
  const announce = useCallback((text: string) => setMessage((last) => ({ text, serial: last.serial + 1 })), []);
  const value = useMemo(() => announce, [announce]);
  return (
    <AnnounceContext.Provider value={value}>
      {children}
      <div id="announcement" className="visually-hidden" role="status" aria-live="polite">
        {message.text ? `${message.text}${message.serial % 2 ? '' : '​'}` : ''}
      </div>
    </AnnounceContext.Provider>
  );
}
