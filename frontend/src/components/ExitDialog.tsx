import { useEffect, useRef, type RefObject } from 'react';

interface Props {
  open: boolean;
  scopeRef: RefObject<HTMLDivElement>;
  onStay: () => void;
  onExit: () => void;
}

/** Titan requires a confirmation before Back exits the app from the home screen. */
export function ExitDialog({ open, scopeRef, onStay, onExit }: Props) {
  const stayRef = useRef<HTMLButtonElement>(null);
  useEffect(() => { if (open) stayRef.current?.focus(); }, [open]);
  if (!open) return null;
  return (
    <div className="dialog" role="dialog" aria-modal="true" aria-labelledby="exit-q" ref={scopeRef}>
      <div className="panel">
        <p id="exit-q">Do you want to exit?</p>
        <div className="actions">
          <button className="btn f" ref={stayRef} onClick={onStay}>Stay</button>
          <button className="btn danger f" onClick={onExit}>Exit</button>
        </div>
      </div>
    </div>
  );
}
