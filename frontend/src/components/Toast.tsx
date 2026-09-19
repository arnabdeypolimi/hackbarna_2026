import { useCallback, useEffect, useRef, useState } from 'react';

/** 'alert' is for something that did not happen; everything else just reports. */
export type ToastKind = 'info' | 'alert';

export function useToast() {
  const [message, setMessage] = useState('');
  const [kind, setKind] = useState<ToastKind>('info');
  const [visible, setVisible] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  const show = useCallback((m: string, k: ToastKind = 'info') => {
    setMessage(m);
    setKind(k);
    setVisible(true);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setVisible(false), 2600);
  }, []);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  return { message, kind, visible, show };
}

export function Toast({ message, kind, visible }: { message: string; kind: ToastKind; visible: boolean }) {
  return (
    <div className={`toast ${kind}${visible ? ' show' : ''}`} role="status" aria-live="polite">
      {message}
    </div>
  );
}
