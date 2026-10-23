/**
 * Every submittable Alpha from every task, on the same pane a single task's results use.
 */

import { useMutation, useQuery } from '@tanstack/react-query'
import { PlayIcon, SearchIcon } from 'lucide-react'
import { useMemo, useState } from 'react'
import type { components } from '@/api/generated'
import { CORE_ORDER, DASH, fmt } from '@/lib/format'
import { useDebounced } from '@/lib/use-debounced'
import { useRefetchOn } from '@/lib/ws'
import { Correlation } from '@/screens/alpha-table/columns'
import { AlphaPane } from '@/screens/tasks/alpha-pane'
import { labTasks, type RankedAlpha } from '@/screens/tasks/api'
import { TASK_OFFER } from '@/screens/tasks/columns'
import { usePowerPool } from '@/screens/tasks/power-pool'
import { Button, Chips, Disclosure, ErrorNotice, Fieldset, Input, Panel } from '@/ui/kit'
import { type Column, DataTable, type Sort } from '@/ui/table'

type SubmissionPick = components['schemas']['SubmissionPick']

/** Displayed bounds are converted to the fraction stored in BRAIN's figures. */
const BOUNDS: { key: string; label: string; div: number }[] = [
  { key: 'sharpe', label: 'Sharpe', div: 1 },
  { key: 'fitness', label: 'Fitness', div: 1 },
  { key: 'turnover', label: 'Turnover %', div: 100 },
  { key: 'returns', label: 'Returns %', div: 100 },
  { key: 'drawdown', label: 'Drawdown %', div: 100 },
  { key: 'margin', label: 'Margin bps', div: 10_000 },
  { key: 'operator_count', label: 'Operators', div: 1 },
  { key: 'testSharpe', label: 'Test Sharpe', div: 1 },
  { key: 'testFitness', label: 'Test Fitness', div: 1 },
  { key: 'afterCostSharpe', label: 'After-Cost Sharpe (5 bps)', div: 1 },
  { key: 'prodCorrelation', label: 'Production Correlation', div: 1 },
  { key: 'powerPoolCorrelation', label: 'Power Pool Correlation', div: 1 },
]

const parseNum = (text: string): number | undefined => {
  const value = Number(text)
  return text.trim() === '' || !Number.isFinite(value) ? undefined : value
}

const unique = (values: (string | null | undefined)[]) => [
  ...new Set(values.filter((value): value is string => value != null && value !== '')),
]

const settingText = (row: RankedAlpha, key: string) => {
  const value = row.settings?.[key]
  return value == null ? '' : String(value)
}

const metricValue = (
  row: RankedAlpha,
  key: string,
  powerPoolCorrelation?: number | null,
): number | null => {
  if (key === 'powerPoolCorrelation') return powerPoolCorrelation ?? null
  if (key === 'operator_count') {
    if (row.expression == null) return null
    return (row.expression.match(/\b[A-Za-z_]\w*\s*\(/g) ?? []).length
  }
  const value = (row as unknown as Record<string, unknown>)[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

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
  const [regions, setRegions] = useState<string[]>([])
  const [delays, setDelays] = useState<string[]>([])
  const [universes, setUniverses] = useState<string[]>([])
  const [investabilities, setInvestabilities] = useState<string[]>([])
  const [neutralizations, setNeutralizations] = useState<string[]>([])
  const [bounds, setBounds] = useState<Record<string, { min: string; max: string }>>({})
  const quietSearch = useDebounced(search, 300)
  const quietBounds = useDebounced(bounds, 400)
  const rows = useMemo<RankedAlpha[]>(() => query.data ?? [], [query.data])
  const pool = usePowerPool(rows.flatMap((row) => (row.alphaId ? [row.alphaId] : [])))

  const regionItems = unique(rows.map((row) => settingText(row, 'region'))).map((value) => ({
    value, label: value,
  }))
  const delayItems = unique(rows.map((row) => settingText(row, 'delay'))).map((value) => ({
    value, label: /^D/i.test(value) ? value : `D${value}`,
  }))
  const universeItems = unique(rows.map((row) => settingText(row, 'universe'))).map((value) => ({
    value, label: value,
  }))
  const investabilityItems = [
    { value: 'max_trade', label: 'Max Trade' },
    { value: 'max_position', label: 'Max Position' },
    { value: 'none', label: 'None' },
  ]
  const neutralizationItems = unique(rows.map((row) => settingText(row, 'neutralization'))).map(
    (value) => ({ value, label: value }),
  )

  const filteredRows = useMemo(() => {
    const needle = quietSearch.trim().toLowerCase()
    return rows.filter((row) => {
      if (needle && ![row.alphaId, row.expression].some((value) => value?.toLowerCase().includes(needle))) {
        return false
      }
      const region = settingText(row, 'region')
      const delay = settingText(row, 'delay')
      const universe = settingText(row, 'universe')
      const neutralization = settingText(row, 'neutralization')
      const maxTrade = settingText(row, 'maxTrade') === 'ON'
      const maxPosition = settingText(row, 'maxPosition') === 'ON'
      const investability = maxTrade ? 'max_trade' : maxPosition ? 'max_position' : 'none'
      if (regions.length && !regions.includes(region)) return false
      if (delays.length && !delays.includes(delay)) return false
      if (universes.length && !universes.includes(universe)) return false
      if (investabilities.length && !investabilities.includes(investability)) return false
      if (neutralizations.length && !neutralizations.includes(neutralization)) return false

      for (const bound of BOUNDS) {
        const min = parseNum(quietBounds[bound.key]?.min ?? '')
        const max = parseNum(quietBounds[bound.key]?.max ?? '')
        if (min === undefined && max === undefined) continue
        const poolCorrelation = row.alphaId == null ? null : (pool.by.get(row.alphaId)?.correlation ?? null)
        const value = metricValue(row, bound.key, poolCorrelation)
        if (value === null) return false
        if (min !== undefined && value < min / bound.div) return false
        if (max !== undefined && value > max / bound.div) return false
      }
      return true
    })
  }, [rows, quietSearch, quietBounds, regions, delays, universes, investabilities, neutralizations, pool.by])

  const activeFilters =
    BOUNDS.filter((bound) => bounds[bound.key]?.min || bounds[bound.key]?.max).length +
    regions.length + delays.length + universes.length + investabilities.length + neutralizations.length

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-3">
        <div className="relative w-full max-w-sm">
          <SearchIcon className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-ink-tertiary" aria-hidden />
          <Input
            aria-label="Search submittable Alpha id or expression"
            placeholder="Search id or expression"
            className="num pl-8"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
        </div>
        <Disclosure summary={activeFilters ? `Filters (${activeFilters} active)` : 'Filters'}>
          <div className="flex flex-col gap-4">
            <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
              <Fieldset legend="Region"><Chips label="Region" items={regionItems} value={regions} onChange={setRegions} /></Fieldset>
              <Fieldset legend="Delay"><Chips label="Delay" items={delayItems} value={delays} onChange={setDelays} /></Fieldset>
              <Fieldset legend="Universe"><Chips label="Universe" items={universeItems} value={universes} onChange={setUniverses} /></Fieldset>
              <Fieldset legend="Investability"><Chips label="Investability" items={investabilityItems} value={investabilities} onChange={setInvestabilities} /></Fieldset>
              <Fieldset legend="Neutralization"><Chips label="Neutralization" items={neutralizationItems} value={neutralizations} onChange={setNeutralizations} /></Fieldset>
            </div>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 md:grid-cols-4">
              {BOUNDS.map((bound) => (
                <Fieldset key={bound.key} legend={bound.label}>
                  <div className="flex gap-1.5">
                    {(['min', 'max'] as const).map((side) => (
                      <Input
                        key={side}
                        type="number"
                        inputMode="decimal"
                        step="any"
                        aria-label={`${side === 'min' ? 'Minimum' : 'Maximum'} ${bound.label}`}
                        placeholder={side}
                        value={bounds[bound.key]?.[side] ?? ''}
                        onChange={(event) => setBounds({
                          ...bounds,
                          [bound.key]: {
                            ...(bounds[bound.key] ?? { min: '', max: '' }),
                            [side]: event.target.value,
                          },
                        })}
                      />
                    ))}
                  </div>
                </Fieldset>
              ))}
            </div>
          </div>
        </Disclosure>
        <p className="text-caption text-ink-subtle">
          Showing <span className="num">{fmt.int(filteredRows.length)}</span> of{' '}
          <span className="num">{fmt.int(rows.length)}</span> submittable Alphas
        </p>
      </div>
      <AlphaPane
        title="Submittable Alphas"
        view="submittable"
        rows={filteredRows}
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
        alphaIds={filteredRows.flatMap((r) => r.alphaId ?? [])}
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

  return (
    <Panel
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
        </>
      )}
    </Panel>
  )
}
