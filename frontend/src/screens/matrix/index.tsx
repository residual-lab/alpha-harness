/**
 * The live 8×10 simulation matrix: one row per BRAIN slot ("core"), one cell per Alpha
 * inside its multi-simulation. Each core shows the five-part batch key every Alpha in its
 * multi-simulation must share. QUEUED work is counted, never drawn.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ClockIcon, EllipsisIcon } from 'lucide-react'
import { type KeyboardEvent, useMemo, useRef, useState } from 'react'
import { toast } from 'sonner'
import { simulations } from '@/api/core'
import { MULTI_SIMULATION_SIZE, SIMULATION_SLOTS } from '@/api/platform'
import type { SimulationRow } from '@/api/types'
import { cn } from '@/lib/cn'
import { DASH, fmt, secondsSince } from '@/lib/format'
import { useCores, useLive } from '@/lib/live'
import { blockLabel, type Cell, type CoreBlock, coreBlocks, EMPTY } from '@/lib/matrix'
import { useNow } from '@/lib/now'
import { useRefetchOn } from '@/lib/ws'
import {
  Button,
  ErrorNotice,
  Notice,
  Page,
  PageHeader,
  Panel,
  Progress,
  Skeleton,
  STATUS,
} from '@/ui/kit'
import { Confirm, Menu, Tooltip } from '@/ui/overlay'

export function MatrixScreen() {
  return (
    <Page>
      <PageHeader title="Simulation Matrix" description="What each core is running right now." />
      <SimulationMatrix />
    </Page>
  )
}

/** CLAUDE.md §4.1: the fields BRAIN requires every child of one multi-simulation to share. */
const BATCH_KEY: { label: string; value: (row: SimulationRow) => string }[] = [
  { label: 'Type', value: (r) => r.simType },
  { label: 'Region', value: (r) => r.region },
  { label: 'Delay', value: (r) => String(r.delay) },
  { label: 'Instrument Type', value: (r) => r.instrumentType },
  { label: 'Language', value: (r) => r.language },
]

/** How long a batch has been out. Its own component, so the second hand touches one span per
 * core rather than re-rendering all eighty cells every second. */
function Elapsed({ since }: { since: string | null | undefined }) {
  const now = useNow(since ? 1000 : 0)
  return (
    <span className="num min-w-12 text-right text-body-compact font-medium text-ink">
      {fmt.duration(secondsSince(since, now))}
    </span>
  )
}

/** One tab stop for the whole matrix, arrows between the Alphas in it, so getting past it never
 * costs eighty presses. Empty slots are left out of the walk entirely. */
interface Roving {
  current: number
  cells: { current: (HTMLElement | null)[] }
  onKeyDown: (event: KeyboardEvent<HTMLElement>) => void
}

function useRovingCells(count: number): Roving {
  const [index, setIndex] = useState(0)
  const cells = useRef<(HTMLElement | null)[]>([])
  // A finished Alpha shortens the list; keep the stop inside it rather than past the end.
  const current = count === 0 ? 0 : Math.min(index, count - 1)

  const move = (next: number) => {
    setIndex(next)
    cells.current[next]?.focus()
  }
  const onKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (count === 0) return
    const step =
      event.key === 'ArrowRight' || event.key === 'ArrowDown'
        ? 1
        : event.key === 'ArrowLeft' || event.key === 'ArrowUp'
          ? -1
          : 0
    if (step !== 0) {
      event.preventDefault()
      move((current + step + count) % count)
    } else if (event.key === 'Home') {
      event.preventDefault()
      move(0)
    } else if (event.key === 'End') {
      event.preventDefault()
      move(count - 1)
    }
    // Anything else, Tab included, is left alone: it leaves the matrix.
  }
  return { current, cells, onKeyDown }
}

function SimulationMatrix() {
  const queryClient = useQueryClient()
  const live = useLive((s) => s.simulations)
  // Until the socket's first snapshot lands, read the active set once over REST.
  const fallback = useQuery({
    queryKey: ['simulations', 'active'],
    queryFn: () => simulations.active(),
    enabled: live === null,
  })
  const engine = useQuery({
    queryKey: ['simulations', 'engine'],
    queryFn: () => simulations.engine(),
  })
  useRefetchOn('simulations', ['simulations', 'engine'], 2000)

  const active = live ?? fallback.data
  const status = engine.data
  const slots = status?.slots ?? SIMULATION_SLOTS
  const maxBatch = status?.maxBatch ?? MULTI_SIMULATION_SIZE
  const { cores, overflow } = useCores(active, slots, maxBatch)
  // One row per run rather than per core: a GLB run holds two cores and a region-agnostic one
  // three, and drawing the rest as idle rows would show a matrix with room it does not have.
  const blocks = useMemo(() => coreBlocks(cores, slots), [cores, slots])
  // The Alphas actually drawn, numbered across the whole matrix, so one list can be walked.
  const perRow = blocks.map((block) =>
    block.core?.holder ? block.core.cells.filter((cell) => cell.state !== 'EMPTY').length : 0,
  )
  const firstCellOfRow = perRow.map((_, i) => perRow.slice(0, i).reduce((a, b) => a + b, 0))
  const roving = useRovingCells(perRow.reduce((a, b) => a + b, 0))

  const [target, setTarget] = useState<SimulationRow | null>(null)
  const cancel = useMutation({
    // Always the parent record: a batch child cannot be cancelled on BRAIN.
    mutationFn: (row: SimulationRow) => simulations.cancel(row.id),
    onSuccess: (result, row) => {
      const what = `${row.isBatch ? 'batch' : 'simulation'} ${row.platformId ?? row.id}`
      if (result.acknowledged) toast.success(`BRAIN acknowledged the cancel of ${what}.`)
      else toast.error(`BRAIN did not acknowledge the cancel of ${what}. It may still be running.`)
      for (const queryKey of [['simulations'], ['bar'], ['today']])
        void queryClient.invalidateQueries({ queryKey })
    },
    onSettled: () => setTarget(null),
  })

  return (
    <Panel
      title={`${slots} cores × ${maxBatch} Alphas`}
      description="Every Alpha in one batch shares the same Type, Region, Delay, Instrument Type and Language. Only simulations that have started are shown here. Waiting ones start as soon as a core is free; the queued count shows how many are waiting."
      actions={
        status && (
          <div className="flex flex-wrap items-center gap-2 text-body-compact">
            <span className={STATUS}>
              <span className="num text-ink">
                {fmt.int(status.slotsUsed)}/{fmt.int(status.slots)}
              </span>
              cores busy
            </span>
            <span className={STATUS}>
              <span className="num text-ink">{fmt.int(status.queuedTotal)}</span>
              queued
            </span>
            <span className={STATUS}>
              <span className="num text-ink">{fmt.int(status.maxBatch)}</span>
              per batch
            </span>
          </div>
        )
      }
      bodyClassName="flex flex-col gap-4"
    >
      {status?.dailyLimitHit && (
        <Notice tone="warn" title="BRAIN's daily simulation limit is reached">
          Nothing more will be sent until the allowance resets at midnight US Eastern. Queued work
          stays queued.
        </Notice>
      )}
      {status?.sessionLost && (
        <Notice tone="warn" title="BRAIN is not accepting your session">
          Sending is paused and queued work stays queued. It resumes by itself once the session is
          renewed, or as soon as you sign in again.
        </Notice>
      )}
      {engine.isError && (
        <ErrorNotice error={engine.error} title="The engine status could not load" />
      )}
      {live === null && fallback.isError && (
        <ErrorNotice error={fallback.error} title="Running simulations could not load" />
      )}
      {overflow > 0 && (
        <Notice tone="warn">
          <span className="num">{fmt.int(overflow)}</span> running{' '}
          {overflow === 1 ? 'simulation does' : 'simulations do'} not fit in the{' '}
          <span className="num">{slots}</span> cores and {overflow === 1 ? 'is' : 'are'} not drawn.
        </Notice>
      )}

      {!active ? (
        !(live === null && fallback.isError) && (
          <div className="flex flex-col gap-2">
            {Array.from({ length: slots }, (_, i) => (
              <Skeleton
                key={i}
                className="h-20"
                {...(i === 0 && { label: 'Loading the Simulation Matrix' })}
              />
            ))}
          </div>
        )
      ) : (
        <div
          role="grid"
          aria-label="Cores and the Alphas running on them"
          className="flex flex-col"
          onKeyDown={roving.onKeyDown}
        >
          {blocks.map((block, i) => (
            <CoreRow
              // By what it runs: a row keyed by position handed an open menu to the next batch
              // when the one it was opened on finished.
              key={block.holder ? `sim-${block.holder.id}` : `idle-${block.start}`}
              block={block}
              maxBatch={maxBatch}
              onCancel={setTarget}
              roving={roving}
              firstCell={firstCellOfRow[i] ?? 0}
            />
          ))}
        </div>
      )}

      <Confirm
        open={target !== null}
        onOpenChange={(open) => !open && !cancel.isPending && setTarget(null)}
        title={target?.isBatch ? 'Cancel this batch?' : 'Cancel this simulation?'}
        confirmLabel="Cancel on BRAIN"
        cancelLabel="Keep running"
        danger
        pending={cancel.isPending}
        onConfirm={() => target && cancel.mutate(target)}
      >
        {target?.isBatch ? (
          <>
            Asks BRAIN to cancel batch{' '}
            <span className="num text-ink">{target.platformId ?? target.id}</span>
            {target.expression ? ` (${target.expression})` : ''}. None of its Alphas will finish.
          </>
        ) : (
          <>
            Asks BRAIN to cancel simulation{' '}
            <span className="num text-ink">{target?.platformId ?? target?.id}</span>. It will not
            finish.
          </>
        )}
      </Confirm>
    </Panel>
  )
}

function CoreRow({
  block,
  maxBatch,
  onCancel,
  roving,
  firstCell,
}: {
  block: CoreBlock
  maxBatch: number
  onCancel: (row: SimulationRow) => void
  roving: Roving
  firstCell: number
}) {
  const holder = block.holder
  const core = block.core
  const label = blockLabel(block)
  // Numbered within the matrix, not the row: the empty slots are not part of the walk.
  let cellNumber = firstCell
  return (
    // -1: part of the grid a screen reader walks, never its own tab stop.
    <div
      role="row"
      tabIndex={-1}
      className="flex flex-col gap-2 border-b border-hairline-subtle py-3 first:pt-0 last:border-b-0 last:pb-0"
    >
      <div
        role="gridcell"
        tabIndex={-1}
        className="flex flex-wrap items-center gap-x-3 gap-y-2 text-body-compact"
      >
        {/* A run that holds several cores names them all, so the row is not mistaken for
            one core while the rest look free. */}
        <span
          className="num flex h-7 shrink-0 items-center justify-center rounded-xs border border-hairline-strong bg-surface-2 px-2 text-caption font-medium text-ink"
          title={block.span > 1 ? `One simulation holding ${block.span} cores` : undefined}
        >
          {label}
        </span>

        <div
          role="group"
          aria-label={`${label} batch key`}
          className={cn(
            'flex min-h-7 flex-wrap items-center gap-x-4 rounded-md border px-3',
            holder ? 'border-hairline-strong bg-surface-2' : 'border-hairline bg-surface-1',
          )}
        >
          {BATCH_KEY.map((key) => (
            <span key={key.label} className="flex items-center gap-1.5 whitespace-nowrap">
              <span className="text-caption uppercase text-ink-subtle">{key.label}</span>
              <span className={cn('num', holder ? 'text-ink font-medium' : 'text-ink-subtle')}>
                {holder ? key.value(holder) : DASH}
              </span>
            </span>
          ))}
        </div>

        {holder ? (
          <div className="ml-auto flex min-w-0 flex-wrap items-center gap-3">
            <span className="max-w-56 truncate text-ink-muted" title={holder.task}>
              {holder.task}
            </span>
            <span className="num shrink-0 text-ink-subtle" title={holder.platformId ?? undefined}>
              {holder.platformId
                ? holder.platformId.length > 8
                  ? `${holder.platformId.slice(0, 8)}…`
                  : holder.platformId
                : 'pending'}
            </span>
            <span title="Progress reported by BRAIN" className={cn(STATUS, 'shrink-0 gap-2')}>
              <Progress value={holder.progress} className="w-16" label="Progress" />
              <span className="num w-10 text-right text-body-compact font-medium text-ink">
                {fmt.pct(holder.progress, 0)}
              </span>
            </span>
            <span title="Time since this batch started" className={cn(STATUS, 'shrink-0')}>
              <ClockIcon className="size-3.5 shrink-0" aria-hidden />
              <Elapsed since={holder.submittedAt} />
            </span>
            <Menu
              trigger={
                <Button variant="ghost" size="icon-sm" aria-label={`${label} actions`}>
                  <EllipsisIcon />
                </Button>
              }
              items={[
                {
                  label: holder.isBatch ? 'Cancel batch' : 'Cancel simulation',
                  danger: true,
                  onClick: () => onCancel(holder),
                },
              ]}
            />
          </div>
        ) : (
          <span className="ml-auto text-caption uppercase tracking-wider text-ink-subtle">
            Idle
          </span>
        )}
      </div>

      {/* presentation: the cells below belong to the row, not to this wrapper. */}
      <div
        role="presentation"
        className="grid min-w-0 gap-1.5"
        style={{ gridTemplateColumns: `repeat(${maxBatch}, minmax(0, 1fr))` }}
      >
        {(core?.cells ?? Array.from({ length: maxBatch }, () => EMPTY)).map((cell, j) => {
          const drawn = holder !== null && cell.state !== 'EMPTY'
          const at = drawn ? cellNumber++ : null
          return <MatrixCell key={j} cell={cell} holder={holder} at={at} roving={roving} />
        })}
      </div>
    </div>
  )
}

/** No per-cell clock: every Alpha in a batch starts and ends with it, so the time lives once on the core row. */
function MatrixCell({
  cell,
  holder,
  at,
  roving,
}: {
  cell: Cell
  holder: SimulationRow | null
  /** Its place in the matrix-wide walk, or null when the slot is empty. */
  at: number | null
  roving: Roving
}) {
  const className = cn(
    'num flex h-9 min-w-0 items-center justify-center truncate rounded-sm px-1 text-body-compact transition-colors',
    // Running carries its state in a status-running dot, not an outline that would read as a button.
    cell.state === 'RUNNING' && 'border border-hairline-strong bg-ink-tint',
    cell.state === 'PENDING' && 'border border-hairline bg-surface-2 text-ink-subtle',
    cell.state === 'EMPTY' && 'border border-hairline bg-canvas',
  )
  if (cell.state === 'EMPTY' || !holder) return <div className={className} aria-hidden />

  const row = cell.row
  return (
    <Tooltip
      content={
        <div className="flex flex-col gap-1">
          <span className="num break-all text-ink">
            {row?.expression ?? 'Not linked to its batch yet'}
          </span>
          <span>
            {cell.state === 'RUNNING' ? 'Running' : 'Pending'} · Universe{' '}
            <span className="num">{row?.universe ?? row?.settings?.universe ?? DASH}</span> ·
            Neutralization <span className="num">{row?.settings?.neutralization ?? DASH}</span>
          </span>
          <span>
            Task <span className="num">{holder.task}</span> · batch{' '}
            <span className="num">{holder.platformId ?? 'pending'}</span>
          </span>
        </div>
      }
    >
      <div
        role="gridcell"
        className={className}
        ref={(node) => {
          if (at !== null) roving.cells.current[at] = node
        }}
        // One stop for the whole matrix: the rest are reached with the arrow keys.
        tabIndex={at === roving.current ? 0 : -1}
        aria-label={
          row?.expression ?? (cell.state === 'RUNNING' ? 'Running Alpha' : 'Pending Alpha')
        }
      >
        {cell.state === 'RUNNING' && <span className="size-1.5 rounded-pill bg-status-running" />}
      </div>
    </Tooltip>
  )
}
