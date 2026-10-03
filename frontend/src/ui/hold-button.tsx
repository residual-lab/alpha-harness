/**
 * A button that acts only once held down, for what cannot be undone. The fill shows how long
 * is left; letting go early does nothing. Space and Enter hold it the same way.
 */

import { type ReactNode, useEffect, useRef, useState } from 'react'
import { cn } from '@/lib/cn'
import { Button } from './kit'

export function HoldButton({
  onHold,
  label,
  children,
  ms = 1200,
  size = 'sm',
  pending,
  disabled,
  className,
}: {
  onHold: () => void
  /** What it does, for its name and tooltip: "Delete task". */
  label: string
  children: ReactNode
  ms?: number
  size?: 'sm' | 'md' | 'icon-sm'
  pending?: boolean
  disabled?: boolean
  className?: string
}) {
  const [holding, setHolding] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined)
  const begin = () => {
    if (disabled || pending || holding) return
    setHolding(true)
    timer.current = setTimeout(() => {
      setHolding(false)
      onHold()
    }, ms)
  }
  const end = () => {
    clearTimeout(timer.current)
    setHolding(false)
  }
  useEffect(() => () => clearTimeout(timer.current), [])
  const hold = (e: React.KeyboardEvent) => e.key === ' ' || e.key === 'Enter'

  return (
    <Button
      variant="danger"
      size={size}
      aria-label={`Hold to ${label.toLowerCase()}`}
      title={`Hold to ${label.toLowerCase()}`}
      disabled={disabled}
      loading={pending}
      className={cn('relative overflow-hidden', className)}
      onPointerDown={begin}
      onPointerUp={end}
      onPointerLeave={end}
      onPointerCancel={end}
      onClick={(e) => e.stopPropagation()}
      onKeyDown={(e) => {
        if (!hold(e) || e.repeat) return
        e.preventDefault()
        begin()
      }}
      onKeyUp={(e) => hold(e) && end()}
    >
      <span
        aria-hidden
        className="absolute inset-y-0 left-0 bg-pnl-negative-tint"
        style={{
          width: holding ? '100%' : '0%',
          transition: holding ? `width ${ms}ms linear` : 'none',
        }}
      />
      <span className="relative inline-flex items-center gap-1.5">{children}</span>
    </Button>
  )
}
