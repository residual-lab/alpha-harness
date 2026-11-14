/**
 * Every submittable Alpha from every task, on the same pane a single task's results use.
 */

import { useMutation, useQuery } from '@tanstack/react-query'
import { PlayIcon, SearchIcon } from 'lucide-react'
import { useMemo, useState } from 'react'
import type { components } from '@/api/generated'
import { IMPLAUSIBLE_SHARPE } from '@/api/platform'
import { CORE_ORDER, DASH, fmt } from '@/lib/format'
import { useDebounced } from '@/lib/use-debounced'
import { useRefetchOn } from '@/lib/ws'
import { Correlation, INVESTABILITY } from '@/screens/alpha-table/columns'
import { type AlphaTableRow, fromRanked } from '@/screens/alpha-table/row'
import { AlphaPane } from '@/screens/tasks/alpha-pane'
import { labTasks, type RankedAlpha } from '@/screens/tasks/api'
import { TASK_OFFER } from '@/screens/tasks/columns'
import { usePowerPool } from '@/screens/tasks/power-pool'
import { Button, Chips, Disclosure, ErrorNotice, Field, Input, Panel } from '@/ui/kit'
import { type Column, DataTable, type Sort } from '@/ui/table'

type SubmissionPick = components['schemas']['SubmissionPick']
type Blocked = components['schemas']['Blocked']
/** A blocked Alpha that passes if submitted first, beside the pick it would go ahead of. */
type PassesFirst = Blocked & { turn: number; bySharpe: number | null | undefined }

/** A task's columns, minus Checks Failed — every row here has none — plus the task. */
const OFFER = [...TASK_OFFER.filter((k) => k !== 'failedChecks'), 'taskName']

const DEFAULTS = [
  'alphaId',
  'taskName',
  'region',
  'delay',
  'universe',
  'neutralization',
  'investability',
  'powerPoolCorrelation',
  'prodCorrelation',
  ...CORE_ORDER,
  'afterCostSharpe',
  'testSharpe',
  'testFitness',
]

/** The settings an Alpha can be picked by, as the table shows them. */
const PICKS = [
  { key: 'region', label: 'Region', text: (v: string) => v },
  { key: 'delay', label: 'Delay', text: (v: string) => `D${v}` },
  { key: 'universe', label: 'Universe', text: (v: string) => v },
  { key: 'neutralization', label: 'Neutralization', text: (v: string) => v },
  {
    key: 'investability',
    label: 'Investability',
    text: (v: string) => INVESTABILITY[v as keyof typeof INVESTABILITY] ?? v,
  },
] as const
type PickKey = (typeof PICKS)[number]['key']

/** Bounds as typed; `div` converts the display unit to the fraction BRAIN's figures are in. */
const BOUNDS = [
  { key: 'sharpe', label: 'Sharpe', div: 1 },
  { key: 'fitness', label: 'Fitness', div: 1 },
  { key: 'turnover', label: 'Turnover %', div: 100 },
  { key: 'returns', label: 'Returns %', div: 100 },
  { key: 'drawdown', label: 'Drawdown %', div: 100 },
  { key: 'margin', label: 'Margin bps', div: 10_000 },
  { key: 'testSharpe', label: 'Test Sharpe', div: 1 },
  { key: 'testFitness', label: 'Test Fitness', div: 1 },
  { key: 'afterCostSharpe', label: 'After-Cost Sharpe', div: 1 },
  { key: 'prodCorrelation', label: 'Production Correlation', div: 1 },
  { key: 'powerPoolCorrelation', label: 'Power Pool Correlation', div: 1 },
] as const
type BoundKey = (typeof BOUNDS)[number]['key']
type Bounds = Partial<Record<BoundKey, { min: string; max: string }>>

/** A Sharpe beyond belief in any period: a straight line or a step, not a signal. */
const implausible = (r: RankedAlpha) =>
  [r.sharpe, r.trainSharpe, r.testSharpe].some((v) => v != null && Math.abs(v) > IMPLAUSIBLE_SHARPE)

/** A typed bound, or null while the box is empty or not a number. */
const bound = (text: string | undefined) => {
  const value = Number.parseFloat(text ?? '')
  return text == null || text.trim() === '' || Number.isNaN(value) ? null : value
}

const pickOf = (row: AlphaTableRow, key: PickKey) => {
  const value = row[key]
  return value == null || value === '' ? '' : String(value)
}

export function SubmittableAlphas({
  onOpenAlpha,
  openAlpha,
}: {
  onOpenAlpha: (alphaId: string, ids?: string[]) => void
  openAlpha: string | null
}) {
  const query = useQuery({ queryKey: ['submittable-alphas'], queryFn: labTasks.submittable })
  // Its own key, refreshed at most every 30s: reading every task's Alphas takes seconds on a
  // large account, too long to redo on each of the Tasks screen's two-second updates.
  useRefetchOn('studies', ['submittable-alphas'], 30_000)
  const [sort, setSort] = useState<Sort>({ key: 'sharpe', desc: true })
  const [search, setSearch] = useState('')
  const [picked, setPicked] = useState<Partial<Record<PickKey, string[]>>>({})
  const [bounds, setBounds] = useState<Bounds>({})
  const [showImplausible, setShowImplausible] = useState(false)
  const quietSearch = useDebounced(search, 300)
  const quietBounds = useDebounced(bounds, 400)
  const all = useMemo<RankedAlpha[]>(() => query.data ?? [], [query.data])
  const table = useMemo(() => all.map(fromRanked), [all])
  const offered = useMemo(
    () =>
      Object.fromEntries(
        PICKS.map(({ key }) => [
          key,
          [...new Set(table.map((row) => pickOf(row, key)).filter(Boolean))].sort(),
        ]),
      ) as Record<PickKey, string[]>,
    [table],
  )
  const flagged = useMemo(() => all.filter(implausible).length, [all])
  const limits = useMemo(
    () =>
      BOUNDS.flatMap((b) => {
        const min = bound(quietBounds[b.key]?.min)
        const max = bound(quietBounds[b.key]?.max)
        return min == null && max == null ? [] : [{ ...b, min, max }]
      }),
    [quietBounds],
  )
  // Power Pool Correlation is measured on request, and costs seconds on a large account: only
  // while a bound asks for it.
  const pool = usePowerPool(
    useMemo(
      () =>
        limits.some((b) => b.key === 'powerPoolCorrelation')
          ? all.flatMap((r) => r.alphaId ?? [])
          : [],
      [limits, all],
    ),
  )
  const figure = useMemo(
    () =>
      (row: AlphaTableRow, key: BoundKey): number | null =>
        key === 'powerPoolCorrelation'
          ? (pool.by.get(row.alphaId ?? '')?.correlation ?? null)
          : (row[key] ?? null),
    [pool.by],
  )
  // Every filter at once (AND). A bound leaves out the Alphas without that figure yet: they
  // cannot be shown to meet it, so they are counted beside the bounds instead.
  const rows = useMemo(() => {
    const needle = quietSearch.trim().toLowerCase()
    return all.filter((r, i) => {
      const row = table[i] as AlphaTableRow
      if (!showImplausible && implausible(r)) return false
      if (needle && ![r.alphaId, r.expression].some((v) => v?.toLowerCase().includes(needle)))
        return false
      for (const { key } of PICKS) {
        const wanted = picked[key]
        if (wanted?.length && !wanted.includes(pickOf(row, key))) return false
      }
      return limits.every((b) => {
        const value = figure(row, b.key)
        return (
          value != null &&
          (b.min == null || value >= b.min / b.div) &&
          (b.max == null || value <= b.max / b.div)
        )
      })
    })
  }, [all, table, showImplausible, quietSearch, picked, limits, figure])
  const unmeasured = limits.flatMap((b) => {
    const missing = table.filter((row) => figure(row, b.key) == null).length
    return missing ? [`${fmt.int(missing)} without ${b.label} yet`] : []
  })
  const setBound = (key: BoundKey, side: 'min' | 'max', text: string) =>
    setBounds((now) => ({ ...now, [key]: { min: '', max: '', ...now[key], [side]: text } }))

  return (
    <div className="flex flex-col gap-4">
      <Panel title="Filters" bodyClassName="flex flex-col gap-4">
        <div className="flex flex-wrap items-end gap-4">
          <Field label="Search" className="w-72 min-w-0">
            <div className="relative">
              <SearchIcon
                className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-ink-tertiary"
                aria-hidden
              />
              <Input
                aria-label="Search Alpha id or expression"
                placeholder="Alpha id or expression"
                className="num pl-8"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </div>
          </Field>
          {PICKS.filter(({ key }) => offered[key].length > 1).map(({ key, label, text }) => (
            <Field key={key} label={label} className="min-w-0">
              <Chips
                label={`${label} shown`}
                items={offered[key].map((v) => ({ value: v, label: text(v) }))}
                value={picked[key] ?? []}
                onChange={(value) => setPicked((now) => ({ ...now, [key]: value }))}
              />
            </Field>
          ))}
        </div>
        <Disclosure summary={limits.length ? `Bounds · ${limits.length} set` : 'Bounds'}>
          <div className="grid grid-cols-1 gap-4 p-3 sm:grid-cols-2 lg:grid-cols-4">
            {BOUNDS.map((b) => (
              <Field key={b.key} label={b.label} className="min-w-0">
                <div className="flex gap-1.5">
                  {(['min', 'max'] as const).map((side) => (
                    <Input
                      key={side}
                      type="number"
                      inputMode="decimal"
                      step="any"
                      aria-label={`${side === 'min' ? 'Minimum' : 'Maximum'} ${b.label}`}
                      placeholder={side === 'min' ? 'Min' : 'Max'}
                      value={bounds[b.key]?.[side] ?? ''}
                      onChange={(e) => setBound(b.key, side, e.target.value)}
                    />
                  ))}
                </div>
              </Field>
            ))}
          </div>
          {unmeasured.length > 0 && (
            <p className="px-3 pb-3 text-body-compact text-ink-subtle">
              Left out, since they cannot be shown to meet a bound: {unmeasured.join(', ')}.
            </p>
          )}
        </Disclosure>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-body-compact text-ink-subtle">
          <span>
            Showing <span className="num">{fmt.int(rows.length)}</span> of{' '}
            <span className="num">{fmt.int(all.length)}</span>
          </span>
          {flagged > 0 && (
            <span className="flex items-center gap-2">
              {showImplausible
                ? `Including ${fmt.int(flagged)} with a Sharpe beyond ±${IMPLAUSIBLE_SHARPE} in some period.`
                : `${fmt.int(flagged)} left out for a Sharpe beyond ±${IMPLAUSIBLE_SHARPE} in some period: almost always a straight line.`}
              <Button variant="ghost" size="sm" onClick={() => setShowImplausible((on) => !on)}>
                {showImplausible ? 'Hide them' : 'Show them'}
              </Button>
            </span>
          )}
        </div>
      </Panel>
      <AlphaPane
        title="Submittable Alphas"
        view="submittable"
        rows={rows}
        offer={OFFER}
        defaults={DEFAULTS}
        sort={sort}
        onSort={setSort}
        loading={query.isPending}
        error={query.error}
        onRefresh={() => query.refetch()}
        onOpenAlpha={onOpenAlpha}
        openAlpha={openAlpha}
      />
      <SubmissionOrder
        alphaIds={rows.flatMap((r) => r.alphaId ?? [])}
        onOpenAlpha={onOpenAlpha}
        openAlpha={openAlpha}
      />
    </div>
  )
}

const PICK_COLUMNS: Column<SubmissionPick & { turn: number }>[] = [
  { key: 'turn', header: '#', width: '56px', align: 'right', cell: (r) => fmt.int(r.turn) },
  {
    key: 'alphaId',
    header: 'Alpha',
    width: '120px',
    cell: (r) => <span className="num">{r.alphaId}</span>,
  },
  { key: 'region', header: 'Region', width: '90px', cell: (r) => r.region ?? DASH },
  {
    key: 'afterCostSharpe',
    header: 'After-Cost Sharpe',
    align: 'right',
    cell: (r) => <span className="num">{fmt.ratio(r.afterCostSharpe)}</span>,
  },
  {
    key: 'sharpe',
    header: 'Sharpe',
    align: 'right',
    cell: (r) => <span className="num">{fmt.ratio(r.sharpe)}</span>,
  },
  {
    key: 'correlation',
    header: 'Power Pool Correlation',
    align: 'right',
    cell: (r) => (
      <span className="num">
        <Correlation value={r.correlation} />
      </span>
    ),
  },
  {
    key: 'blocks',
    header: 'Blocks',
    align: 'right',
    cell: (r) => <span className="num">{fmt.int(r.blocks)}</span>,
  },
]

const FIRST_COLUMNS: Column<PassesFirst>[] = [
  {
    key: 'alphaId',
    header: 'Alpha',
    width: '120px',
    cell: (r) => <span className="num">{r.alphaId}</span>,
  },
  {
    key: 'sharpe',
    header: 'Sharpe',
    align: 'right',
    cell: (r) => <span className="num">{fmt.ratio(r.sharpe)}</span>,
  },
  {
    key: 'by',
    header: 'Submit Before',
    cell: (r) => (
      <span className="num">
        #{fmt.int(r.turn)} · {r.by}
      </span>
    ),
  },
  {
    key: 'bySharpe',
    header: 'Its Sharpe',
    align: 'right',
    cell: (r) => <span className="num">{fmt.ratio(r.bySharpe)}</span>,
  },
]

/**
 * Submits greedily without submitting: best After-Cost Sharpe first, each pick joining its
 * region's Power Pool before the rest are judged again, as they would be after a real
 * submission and a Portfolio sync.
 */
function SubmissionOrder({
  alphaIds,
  onOpenAlpha,
  openAlpha,
}: {
  alphaIds: string[]
  onOpenAlpha: (alphaId: string, ids?: string[]) => void
  openAlpha: string | null
}) {
  const order = useMutation({ mutationFn: () => labTasks.powerPoolOrder(alphaIds) })
  const found = order.data
  const picks = useMemo(() => (found?.picks ?? []).map((p, i) => ({ ...p, turn: i + 1 })), [found])
  const first = useMemo(() => {
    const byId = new Map(picks.map((p) => [p.alphaId, p]))
    return (found?.blocked ?? []).flatMap((b): PassesFirst[] => {
      const blocker = byId.get(b.by)
      return b.passesFirst && blocker
        ? [{ ...b, turn: blocker.turn, bySharpe: blocker.sharpe }]
        : []
    })
  }, [found, picks])

  return (
    <Panel
      id="submission-order"
      title="Submission Order"
      description="How many could be submitted one after another, best After-Cost Sharpe first. Each pick joins its region's Power Pool and every Alpha left is judged again against it. Nothing is submitted, and nothing is asked of BRAIN."
      actions={
        <Button
          size="sm"
          variant="secondary"
          disabled={alphaIds.length === 0}
          loading={order.isPending}
          onClick={() => order.mutate()}
        >
          <PlayIcon />
          Simulate
        </Button>
      }
    >
      {order.error != null && (
        <ErrorNotice error={order.error} title="Could not work out the order" />
      )}
      {found && (
        <>
          <p className="num mb-3 text-body-compact text-ink-muted">
            {fmt.int(found.picks.length + found.blocked.length + found.unmeasured.length)} Pass
            Power Pool Correlation today → {fmt.int(found.picks.length)} can be submitted one after
            another · {fmt.int(found.blocked.length)} blocked by an earlier pick
            {first.length > 0 &&
              ` · ${fmt.int(first.length)} would pass if submitted before their blocker`}
            {found.unmeasured.length > 0 &&
              ` · ${fmt.int(found.unmeasured.length)} not measured: no daily PnL stored`}
          </p>
          <DataTable
            label="Submission Order"
            rows={picks}
            columns={PICK_COLUMNS}
            rowKey={(r) => r.alphaId}
            activeKey={openAlpha}
            onRowClick={(r, _event, shown) =>
              onOpenAlpha(
                r.alphaId,
                shown.map((p) => p.alphaId),
              )
            }
            empty="Nothing here passes Power Pool Correlation today."
            maxHeight="50vh"
          />
          {first.length > 0 && (
            <>
              <p className="mt-4 mb-3 text-body-compact text-ink-muted">
                Blocked, but each would still pass if submitted before its blocker, whose Sharpe
                clears 1.10× its own. That keeps both of the pair; a later pick it collides with is
                not judged again.
              </p>
              <DataTable
                label="Would pass if submitted first"
                rows={first}
                columns={FIRST_COLUMNS}
                rowKey={(r) => r.alphaId}
                activeKey={openAlpha}
                onRowClick={(r, _event, shown) =>
                  onOpenAlpha(
                    r.alphaId,
                    shown.map((p) => p.alphaId),
                  )
                }
                maxHeight="30vh"
              />
            </>
          )}
        </>
      )}
    </Panel>
  )
}
