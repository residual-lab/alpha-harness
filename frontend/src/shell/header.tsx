/**
 * The bar above every screen: the clocks a consultant runs against (session, simulations
 * left, queued with the matrix on hover, quota reset), connection trouble, and ⌘K search.
 */

import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { ClockIcon, SearchIcon, ZapIcon } from 'lucide-react'
import { type ReactNode, useEffect, useMemo, useState } from 'react'
import { simulations, today } from '@/api/core'
import { cn } from '@/lib/cn'
import { fmt } from '@/lib/format'
import { useCores, useLive } from '@/lib/live'
import { coreBlocks } from '@/lib/matrix'
import { useNow } from '@/lib/now'
import { useRefetchOn } from '@/lib/ws'
import { Button, Kbd, STATUS } from '@/ui/kit'
import { Tooltip } from '@/ui/overlay'
import { useCommandMenu } from './command-menu'
import { inDesktop, MOD, onMac, WindowControls } from './desktop'
import { SidebarToggle } from './sidebar'

export function Header() {
  const openMenu = useCommandMenu((s) => s.setOpen)

  return (
    // In the desktop app this is the title bar: dragged by any part that is not a control, with
    // the window's buttons at its right end, or macOS's own over its left.
    <header
      data-tauri-drag-region={inDesktop ? 'deep' : undefined}
      className={cn(
        'flex h-12 shrink-0 items-center justify-between gap-3 border-b border-hairline bg-canvas px-4',
        inDesktop && (onMac ? 'pl-20' : 'pr-0'),
      )}
    >
      <div className="flex min-w-0 items-center gap-3">
        <SidebarToggle />
        <HeaderCores />
      </div>
      <div className="flex shrink-0 items-center gap-4">
        <Clocks />
        <ConnectionNotice />
        <Tooltip
          content={
            <span className="flex items-center gap-2">
              Go to a screen or lab <Kbd>{MOD}K</Kbd>
            </span>
          }
        >
          <Button
            size="icon-sm"
            aria-label={`Go to a screen or lab (${MOD}K)`}
            onClick={() => openMenu(true)}
          >
            <SearchIcon />
          </Button>
        </Tooltip>
        {inDesktop && !onMac && <WindowControls />}
      </div>
    </header>
  )
}

/** One tile per concurrent simulation core, always in the top bar (DESIGN.md core-slot). */
function HeaderCores() {
  const live = useLive((s) => s.simulations)
  const active = useQuery({
    queryKey: ['simulations', 'active'],
    queryFn: () => simulations.active(),
    enabled: live === null,
    // Always mounted, so it speaks for every screen: a restarted backend may have no
    // snapshot to replay, and one read taken while it was down would stand forever.
    refetchInterval: 5000,
  })
  const engine = useQuery({
    queryKey: ['simulations', 'engine'],
    queryFn: () => simulations.engine(),
  })
  useRefetchOn('simulations', ['simulations', 'engine'], 2000)

  const slots = engine.data?.slots ?? 8
  const maxBatch = engine.data?.maxBatch ?? 10
  const dailyLimitHit = Boolean(engine.data?.dailyLimitHit)
  const sessionLost = Boolean(engine.data?.sessionLost)
  const { cores } = useCores(live ?? active.data, slots, maxBatch)

  const blocks = useMemo(() => coreBlocks(cores, slots), [cores, slots])

  return (
    <nav
      aria-label={`${slots} simulation cores execution matrix`}
      // Scrolls rather than overlaps the clocks when a phone-width bar cannot hold all eight.
      className="flex min-w-0 items-center gap-1.5 overflow-x-auto rounded-md border border-hairline bg-surface-1 p-1 [scrollbar-width:none]"
    >
      <Link
        to="/matrix"
        className="flex items-center gap-1.5 rounded-xs transition-colors hover:bg-surface-2 focus-visible:-outline-offset-2"
        // No `title`: each core carries its own tooltip, and the browser's native one for
        // the link fired on top of it. The label stays for anyone not using a pointer.
        aria-label="Open Simulation Matrix"
      >
        {blocks.map((block) => {
          const holder = block.holder
          let state: 'running' | 'queued' | 'warning' | 'idle' = 'idle'
          if (dailyLimitHit || sessionLost) {
            state = 'warning'
          } else if (holder) {
            state = holder.status === 'RUNNING' ? 'running' : 'queued'
          }

          const stateClasses = {
            running: 'bg-status-running text-white',
            queued: 'bg-status-queued text-ink',
            warning: 'bg-status-warning text-ink',
            // A hollow socket rather than a grey tile, which read as a disabled button.
            idle: 'border border-dashed border-status-idle bg-surface-1 text-ink-subtle hover:text-ink',
          }[state]

          const where =
            block.span === 1
              ? `Core ${block.start + 1}`
              : `Cores ${block.start + 1}\u2013${block.start + block.span}`
          const label =
            state === 'idle'
              ? 'Idle'
              : state === 'running'
                ? 'Running'
                : state === 'warning'
                  ? 'Warning'
                  : 'Pending'

          const tooltipContent = (
            <div className="flex flex-col gap-1 text-caption">
              <span className="font-medium text-ink">
                {where}: {label}
              </span>
              {holder && (
                <>
                  <span className="text-ink-muted">
                    {holder.region} · D{holder.delay} · {holder.universe}
                  </span>
                  {block.span > 1 && (
                    <span className="text-ink-muted">
                      One simulation holding <span className="num">{block.span}</span> cores
                    </span>
                  )}
                  <span className="num text-ink-subtle">
                    {holder.task} ({fmt.pct(holder.progress, 0)})
                  </span>
                </>
              )}
            </div>
          )

          return (
            <Tooltip
              key={holder ? `sim-${holder.id}` : `idle-${block.start}`}
              content={tooltipContent}
            >
              {/* A hair between a block's own cores and the full gap between blocks, so two
                  cores held by one simulation read as one wide mark rather than two. */}
              <span className="flex gap-px">
                {Array.from({ length: block.span }, (_, k) => (
                  <span
                    key={k}
                    role="status"
                    aria-label={`${where}: ${label}`}
                    className={cn(
                      'num relative flex size-7 shrink-0 items-center justify-center overflow-hidden text-caption font-medium select-none transition-colors max-sm:size-5',
                      stateClasses,
                      block.span === 1
                        ? 'rounded-xs'
                        : k === 0
                          ? 'rounded-l-xs'
                          : k === block.span - 1
                            ? 'rounded-r-xs'
                            : 'rounded-none',
                    )}
                  >
                    <span className="max-sm:hidden">C{block.start + k + 1}</span>
                    <span className="sm:hidden">{block.start + k + 1}</span>
                    {/* Not by colour alone: a warning says so, and a running core fills
                        along its bottom as its simulation goes. */}
                    {state === 'warning' && <span aria-hidden>!</span>}
                    {state === 'running' && holder && (
                      <span
                        aria-hidden
                        className="absolute inset-x-0 bottom-0 h-0.5 origin-left bg-ink transition-transform"
                        style={{
                          transform: `scaleX(${Math.min(1, Math.max(0, holder.progress ?? 0))})`,
                        }}
                      />
                    )}
                  </span>
                ))}
              </span>
            </Tooltip>
          )
        })}
      </Link>
    </nav>
  )
}

/**
 * Silent while live updates flow. Only after the socket has been down for 2s does it say so,
 * so the normal connect on page load never flashes it.
 */
function ConnectionNotice() {
  const connected = useLive((s) => s.connected)
  const [lost, setLost] = useState(false)
  useEffect(() => {
    if (connected) return
    const timer = setTimeout(() => setLost(true), 2000)
    return () => {
      clearTimeout(timer)
      setLost(false)
    }
  }, [connected])

  if (connected || !lost) return null
  return (
    <Tooltip content="Live updates are paused: the connection to the backend dropped. The matrix and counts resume when it reconnects.">
      <span
        role="status"
        className="flex h-7 items-center gap-1.5 rounded-md border border-status-warning-edge bg-status-warning-tint px-3 text-body-compact whitespace-nowrap text-status-warning"
      >
        <span className="size-1.5 animate-pulse rounded-pill bg-status-warning" aria-hidden />
        Reconnecting…
      </span>
    </Tooltip>
  )
}

function Clocks() {
  const bar = useQuery({
    queryKey: ['bar'],
    queryFn: () => today.bar(),
    refetchInterval: 30_000,
  })
  useRefetchOn('simulations', ['bar'], 3000)
  useRefetchOn('session', ['bar'])
  // Seconds since the last fetch, so the countdowns tick between polls.
  const elapsed = Math.max(0, Math.floor((useNow(1000) - bar.dataUpdatedAt) / 1000))
  if (!bar.data) return null

  const session =
    bar.data.expiresInSeconds == null ? null : Math.max(0, bar.data.expiresInSeconds - elapsed)
  // `exact` flips true once today's first simulation POST returns BRAIN's own quota headers.
  const { remaining, exact } = bar.data.simulations

  return (
    // Below 1024px the two clocks hide; below 640px the quota figures drop their visible labels.
    <div className="flex items-center gap-2 text-body-compact text-ink-subtle">
      <Clock
        icon={<ClockIcon className="size-3.5 text-ink-subtle" aria-hidden />}
        label="BRAIN Session"
        hint="BRAIN Session Time To Live"
        wide
      >
        <span
          className={cn(
            'num',
            session !== null && session < 1800 ? 'text-status-warning' : 'text-ink',
          )}
        >
          {fmt.countdown(session)}
        </span>
      </Clock>
      <Clock
        icon={<ZapIcon className="size-3.5 text-primary" aria-hidden />}
        label="Simulations Left Today"
        hint="Simulations Left in Today's Quota"
        valueFirst
      >
        <span className="num font-medium text-ink">
          {exact ? '' : '~'}
          {fmt.int(remaining)}
        </span>
      </Clock>
      <Clock
        icon={<ClockIcon className="size-3.5 text-ink-subtle" aria-hidden />}
        label="Simulation Quota Reset in"
        wide
      >
        <span className="num text-ink">
          {fmt.countdown(Math.max(0, bar.data.resetsInSeconds - elapsed))}
        </span>
      </Clock>
    </div>
  )
}

function Clock({
  icon,
  label,
  hint,
  valueFirst,
  wide,
  children,
}: {
  icon?: ReactNode
  label: string
  hint?: string
  valueFirst?: boolean
  /** Hidden below 1024px. The others stay, showing only their figure below 640px. */
  wide?: boolean
  children: ReactNode
}) {
  const name = <span className="max-sm:sr-only">{label}</span>
  const body = (
    <span
      className={cn(
        STATUS,
        'bg-surface-1 transition-colors hover:border-hairline-strong',
        wide && 'max-lg:hidden',
      )}
    >
      {icon}
      {valueFirst ? children : name}
      {valueFirst ? name : children}
    </span>
  )
  return hint ? <Tooltip content={hint}>{body}</Tooltip> : body
}
