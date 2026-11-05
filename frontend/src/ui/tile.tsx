/** A settings section in a box of its own, whose title can pick or clear the whole section. */

import type { ReactNode } from 'react'
import { cn } from '@/lib/cn'

/**
 * A heading that picks its whole section: clears it when anything in it is on, fills it when
 * it is empty.
 */
export function ToggleTitle({
  label,
  any,
  onToggle,
  className,
}: {
  label: string
  any: boolean
  onToggle: () => void
  className?: string
}) {
  return (
    <button
      type="button"
      aria-pressed={any}
      title={any ? `Clear all ${label}` : `Choose all ${label}`}
      onClick={onToggle}
      className={cn(
        'cursor-pointer whitespace-nowrap underline-offset-4 transition-colors hover:text-ink hover:underline',
        className,
      )}
    >
      {label}
    </button>
  )
}

export function Tile({
  label,
  actions,
  children,
  className,
  any,
  onToggle,
}: {
  label: string
  actions?: ReactNode
  children: ReactNode
  className?: string
  /** With `onToggle`, the heading picks the whole section. */
  any?: boolean
  onToggle?: () => void
}) {
  return (
    <section
      className={cn(
        'flex min-w-0 flex-col gap-2.5 rounded-md border border-hairline-strong bg-surface-2 p-3',
        className,
      )}
    >
      <header className="flex min-h-6 items-center justify-between gap-3">
        <h4 className="text-body-compact font-medium whitespace-nowrap text-ink-muted">
          {onToggle ? <ToggleTitle label={label} any={any ?? false} onToggle={onToggle} /> : label}
        </h4>
        {actions && <div className="flex shrink-0 items-center gap-1">{actions}</div>}
      </header>
      {children}
    </section>
  )
}
