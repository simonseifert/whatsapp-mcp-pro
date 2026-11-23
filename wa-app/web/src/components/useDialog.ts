import { useEffect, type RefObject } from 'react'

/** Modal dialog focus: first control on open, Escape closes, focus returns to whatever opened it. */
export function useDialog(ref: RefObject<HTMLElement | null>, onClose: () => void) {
  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null
    ref.current?.querySelector<HTMLElement>('button, input, select, textarea')?.focus()
    return () => opener?.focus()
  }, [ref])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])
}
