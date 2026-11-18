/**
 * Pyramids: BRAIN's Pyramid Multiplier for every Region · Delay · Dataset Category, and the
 * Alphas submitted in each, in a quarter or ever. A cell opens its market in the Data Explorer.
 */

import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { useNavigate } from '@tanstack/react-router'
import { useMemo, useState } from 'react'
import { ALL_TIME, catalog } from '@/api/catalog'
import type { Scope } from '@/api/types'
import { cn } from '@/lib/cn'
import { DASH, fmt } from '@/lib/format'
import { useScope } from '@/lib/scope'
import { useFieldFilter } from '@/screens/data/state'
import { Empty, ErrorNotice, Page, PageHeader, Panel, Segmented, Skeleton } from '@/ui/kit'
import { Select } from '@/ui/overlay'

const REFRESH_MS = 10 * 60 * 1000

/** Where the ramp tops out unless BRAIN offers more; see {@link ramp}. */
const TOP_MULTIPLIER = 2

/** Deepening green, one step per Pyramid Multiplier; white figures stay at APCA 78 throughout. */
const TINT = [
  'bg-pyramid-0',
  'bg-pyramid-1',
  'bg-pyramid-2',
  'bg-pyramid-3',
  'bg-pyramid-4',
  'bg-pyramid-5',
  'bg-pyramid-6',
  'bg-pyramid-7',
  'bg-pyramid-8',
  'bg-pyramid-9',
  'bg-pyramid-10',
] as const

/**
 * Colour means payout, not rank. A multiplier is a coefficient in a cash formula, so ×1.4 has
 * to look like ×1.4 whatever else the quarter offers — ranking within the grid would paint the
 * best of a lean quarter at full strength and read as a jackpot.
 *
 * Anchored at ×1.0, the floor of standard payout. The ceiling holds at ×2.0 unless BRAIN
 * exceeds it, so a richer scale stretches rather than clipping.
 */
function ramp(multipliers: number[]) {
  const ceiling = Math.max(TOP_MULTIPLIER, ...multipliers)
  return (multiplier: number | null) => {
    if (multiplier == null) return TINT[0]
    const ratio = Math.min(1, Math.max(0, (multiplier - 1) / (ceiling - 1)))
    return TINT[Math.round(ratio * (TINT.length - 1))] ?? TINT[0]
  }
}

/** The strongest multiplier in a row or column, when there is more than one to choose between. */
function peaks(groups: Map<string, number[]>) {
  const best = new Map<string, number>()
  for (const [id, values] of groups) {
    const top = Math.max(...values)
    // Only where something is actually lower: a mark on every cell in a row says nothing.
    if (Math.min(...values) !== top) best.set(id, top)
  }
  return best
}

const key = (categoryId: string, region: string, delay: number) =>
  `${categoryId}|${region}|${delay}`
const times = (m: number | null) => (m == null ? DASH : `×${fmt.ratio(m, 1)}`)

/** An Alpha sits in one pyramid per Dataset Category it uses, so totals count it once in each. */
const TOTAL_NOTE = 'counts an Alpha once in every pyramid it is in'
const TOTAL_CELL = 'num h-8 text-center font-medium text-ink-muted'

const SWATCH = 'h-4 w-6 shrink-0 rounded-xs border border-hairline-strong bg-pyramid-5'

/** What each cell reads out. Two questions about the same grid, never mixed in one figure. */
type View = 'multiplier' | 'alphas'

const VIEWS: { value: View; label: string }[] = [
  { value: 'multiplier', label: 'Multiplier' },
  { value: 'alphas', label: 'Alphas' },
]

/**
 * A pyramid's standing in a quarter, as three states rather than a ramp: BRAIN either counts
 * it as formulated or it does not, and the only other thing worth knowing is how far off it
 * is. Formulating is per quarter, so across all time a count is only submitted or not.
 * Deliberately not the Multiplier green — these are two different readings of one cell,
 * and sharing a palette would let them be mistaken for each other.
 */
function standing(count: number, needed: number, allTime: boolean) {
  if (allTime && count > 0) {
    return {
      face: 'bg-primary-subtle text-ink',
      label: fmt.int(count),
      note: 'formulating counts per quarter',
    }
  }
  if (count >= needed) {
    return {
      face: 'bg-primary font-semibold text-on-primary',
      label: fmt.int(count),
      note: 'formulated',
    }
  }
  if (count > 0) {
    return {
      face: 'bg-primary-subtle text-ink',
      label: `${count}/${needed}`,
      note: `${needed - count} more to formulate`,
    }
  }
  return { face: 'bg-surface-2 text-ink-subtle', label: DASH, note: 'none submitted' }
}

/** Three marks, three meanings: without this the edges are a puzzle rather than a signal. */
function Legend({ view, needed, allTime }: { view: View; needed: number; allTime: boolean }) {
  if (view === 'alphas' && allTime) {
    return (
      <div className="mt-5 flex flex-wrap items-center gap-x-7 gap-y-3 border-t border-hairline pt-4 text-body-compact text-ink-subtle">
        <span className="flex items-center gap-1.5">
          <span className={cn(SWATCH, 'border-hairline-strong bg-surface-2')} />
          none submitted
        </span>
        <span className="flex items-center gap-1.5">
          <span className={cn(SWATCH, 'bg-primary-subtle')} />
          submitted, counted across every quarter
        </span>
        <span>click a cell to browse that pyramid&rsquo;s Data Fields</span>
      </div>
    )
  }
  if (view === 'alphas') {
    return (
      <div className="mt-5 flex flex-wrap items-center gap-x-7 gap-y-3 border-t border-hairline pt-4 text-body-compact text-ink-subtle">
        <span className="flex items-center gap-1.5">
          <span className={cn(SWATCH, 'border-hairline-strong bg-surface-2')} />
          none submitted
        </span>
        <span className="flex items-center gap-1.5">
          <span className={cn(SWATCH, 'bg-primary-subtle')} />
          started, under <span className="num">{needed}</span>
        </span>
        <span className="flex items-center gap-1.5">
          <span className={cn(SWATCH, 'bg-primary')} />
          formulated &mdash; <span className="num">{needed}</span> or more submitted in the quarter
        </span>
        <span>click a cell to browse that pyramid&rsquo;s Data Fields</span>
      </div>
    )
  }
  return (
    <div className="mt-5 flex flex-wrap items-center gap-x-7 gap-y-3 border-t border-hairline pt-4 text-body-compact text-ink-subtle">
      <span className="flex items-center gap-1.5">
        <span className="flex gap-px">
          {[0, 2, 4, 6, 8, 10].map((step) => (
            <span key={step} className={cn('h-4 w-2 shrink-0', TINT[step])} />
          ))}
        </span>
        lower → higher Multiplier
      </span>
      <span className="flex items-center gap-1.5">
        <span className={cn(SWATCH, 'border-t-2 border-b-2 border-t-ink border-b-ink')} />
        best market for this category
      </span>
      <span className="flex items-center gap-1.5">
        <span className={cn(SWATCH, 'border-l-2 border-r-2 border-l-ink border-r-ink')} />
        best category in this market
      </span>
      <span className="flex items-center gap-1.5">
        <span className={cn(SWATCH, 'border-2 border-ink')} />
        best in both
      </span>
      <span>click a cell to browse that pyramid&rsquo;s Data Fields</span>
    </div>
  )
}

/**
 * The universe to open a market in: the one already downloaded, richest first. BRAIN's own
 * map hardcodes a universe per region; reading it off the catalog instead means the market
 * that opens is one the Data Explorer can actually show.
 */
function useSyncedUniverses() {
  const scopes = useQuery({ queryKey: ['catalog', 'scopes'], queryFn: catalog.scopes })
  return useMemo(() => {
    const best = new Map<string, { universe: string; instrumentType: string; fields: number }>()
    for (const s of scopes.data ?? []) {
      const id = `${s.region}-${s.delay}`
      const held = best.get(id)
      if (!held || s.fields > held.fields) {
        best.set(id, { universe: s.universe, instrumentType: s.instrument_type, fields: s.fields })
      }
    }
    return best
  }, [scopes.data])
}

export function PyramidsScreen() {
  // `null` until chosen: BRAIN's quarter in progress, named by the response.
  const [period, setPeriod] = useState<string | null>(null)
  const query = useQuery({
    queryKey: ['catalog', 'pyramids', period],
    queryFn: () => catalog.pyramids(period),
    refetchInterval: REFRESH_MS,
    staleTime: REFRESH_MS / 2,
    placeholderData: keepPreviousData,
  })
  const data = query.data
  const allTime = data?.period === ALL_TIME
  const during = allTime ? 'across all time' : `in ${data?.period ?? 'this quarter'}`
  const periods = useMemo(
    () => [
      { value: ALL_TIME, label: 'All Time' },
      ...(data?.quarters ?? []).map((q, i) => ({
        value: q.name,
        label: i === 0 ? `${q.name} (Current)` : q.name,
      })),
    ],
    [data?.quarters],
  )
  const { cells, tint, bestInRow, bestInColumn, rowTotals, columnTotals, total } = useMemo(() => {
    const all = data?.cells ?? []
    const byRow = new Map<string, number[]>()
    const byColumn = new Map<string, number[]>()
    const multipliers: number[] = []
    const rowTotals = new Map<string, number>()
    const columnTotals = new Map<string, number>()
    let total = 0
    for (const c of all) {
      const id = `${c.region}-${c.delay}`
      rowTotals.set(c.categoryId, (rowTotals.get(c.categoryId) ?? 0) + c.alphaCount)
      columnTotals.set(id, (columnTotals.get(id) ?? 0) + c.alphaCount)
      total += c.alphaCount
      if (c.multiplier == null) continue
      multipliers.push(c.multiplier)
      const row = byRow.get(c.categoryId) ?? []
      row.push(c.multiplier)
      byRow.set(c.categoryId, row)
      const column = byColumn.get(id) ?? []
      column.push(c.multiplier)
      byColumn.set(id, column)
    }
    return {
      cells: new Map(all.map((c) => [key(c.categoryId, c.region, c.delay), c])),
      tint: ramp(multipliers),
      bestInRow: peaks(byRow),
      bestInColumn: peaks(byColumn),
      rowTotals,
      columnTotals,
      total,
    }
  }, [data?.cells])
  const navigate = useNavigate()
  const [, update] = useScope('data')
  const open = (change: Partial<Scope>) => {
    update(change)
    void navigate({ to: '/data' })
  }
  const universes = useSyncedUniverses()
  const [view, setView] = useState<View>('multiplier')
  // Multipliers are rates, so only the Alphas view has anything to add up.
  const totalled = view === 'alphas'
  const needed = data?.alphasPerPyramid ?? 3

  /** A cell is a way in: open its market and narrow the Fields tab to just that category. */
  const openPyramid = (categoryId: string, region: string, delay: number) => {
    const market = universes.get(`${region}-${delay}`)
    if (!market) return
    // Replace rather than merge: a category carried in beside a stale dataset or coverage
    // filter lands the user on an empty table and no clue which filter emptied it.
    useFieldFilter.getState().replace({ category_ids: [categoryId] })
    open({ region, delay, universe: market.universe, instrumentType: market.instrumentType })
  }

  return (
    <Page>
      <PageHeader
        title="Pyramids"
        description="BRAIN's Pyramid Multiplier for every Region, Delay and Dataset Category, and the Alphas you submitted in each."
      />
      <Panel
        title={view === 'alphas' ? 'Pyramid Alpha Distribution' : 'Pyramid Multiplier Map'}
        description={
          view === 'alphas'
            ? `Alphas you submitted in each pyramid ${during}.`
            : 'What BRAIN pays for a submission in each pyramid.'
        }
        actions={
          <div className="flex flex-wrap items-center gap-2">
            {view === 'alphas' && data && (
              <Select
                label="Period"
                mono
                items={periods}
                value={data.period}
                onChange={setPeriod}
              />
            )}
            <Segmented label="Cell value" value={view} onChange={setView} items={VIEWS} />
          </div>
        }
      >
        {query.isError ? (
          <ErrorNotice error={query.error} title="Could not load pyramids from BRAIN" />
        ) : !data ? (
          <Skeleton className="h-96" />
        ) : data.categories.length === 0 ? (
          <Empty title="No Dataset Categories">BRAIN returned no pyramids to show.</Empty>
        ) : (
          <div className="flex gap-2">
            {/* Both axes run richest-first, so the strongest pyramids gather top-left. */}
            <div
              aria-hidden
              className="flex shrink-0 flex-col items-center gap-2 pt-9 pb-1 text-caption text-ink-subtle"
            >
              <span className="border-x-4 border-b-5 border-x-transparent border-b-hairline-strong" />
              <span className="w-px flex-1 bg-hairline-strong" />
              <span className="rotate-180 whitespace-nowrap [writing-mode:vertical-rl]">
                richer categories
              </span>
            </div>
            <div className="min-w-0 flex-1 overflow-x-auto">
              <div
                aria-hidden
                className="mb-2 flex items-center gap-2 text-caption text-ink-subtle"
              >
                <span className="border-y-4 border-r-5 border-y-transparent border-r-hairline-strong" />
                <span className="h-px flex-1 bg-hairline-strong" />
                <span className="shrink-0">richer markets</span>
              </div>
              <table className="w-full min-w-256 table-fixed border-separate border-spacing-1 text-body-compact">
                <thead>
                  <tr>
                    <th scope="col" className="w-32 pr-3 text-left font-medium text-ink-subtle">
                      Category
                    </th>
                    {data.columns.map((column) => (
                      <th
                        key={`${column.region}-${column.delay}`}
                        scope="col"
                        className="num px-1 font-medium whitespace-nowrap text-ink-subtle"
                      >
                        {column.region} D{column.delay}
                      </th>
                    ))}
                    {totalled && (
                      <th
                        scope="col"
                        title={TOTAL_NOTE}
                        className="w-16 px-1 font-medium text-ink-subtle"
                      >
                        Total
                      </th>
                    )}
                  </tr>
                </thead>
                <tbody>
                  {data.categories.map((category) => (
                    <tr key={category.id}>
                      <th
                        scope="row"
                        className="truncate pr-3 text-left font-normal text-ink-muted"
                        title={category.name}
                      >
                        {category.name}
                      </th>
                      {data.columns.map((column) => {
                        const id = `${column.region}-${column.delay}`
                        const cell = cells.get(key(category.id, column.region, column.delay))
                        if (!cell) {
                          return (
                            <td key={id} className="num text-center text-ink-subtle">
                              {DASH}
                            </td>
                          )
                        }
                        // The marks rank multipliers, so they belong to that view alone.
                        const ranked = view === 'multiplier'
                        const topOfRow = ranked && cell.multiplier === bestInRow.get(category.id)
                        const topOfColumn = ranked && cell.multiplier === bestInColumn.get(id)
                        const notes = [
                          topOfRow && 'best market for this category',
                          topOfColumn && 'best category in this market',
                        ].filter(Boolean)
                        const quarter = standing(cell.alphaCount, needed, allTime)
                        const reading = `${category.name} · ${column.region} D${column.delay} · Pyramid Multiplier ${times(cell.multiplier)} · ${fmt.int(cell.alphaCount)} of your Alphas ${during}, ${quarter.note}${notes.length ? ` · ${notes.join(' · ')}` : ''}`
                        // Openable only where this category's Fields are downloaded for the
                        // market: the Data Explorer reads the local catalog, so a cell BRAIN
                        // pays for is still a dead end until the market is synced.
                        const openable = cell.synced && universes.has(id)
                        const face = cn(
                          'num flex h-8 w-full items-center justify-center rounded-sm border whitespace-nowrap',
                          ranked ? cn('text-ink', tint(cell.multiplier)) : quarter.face,
                          // Each mark runs along the axis it belongs to: horizontal rules
                          // frame the row, vertical rules frame the column, and all four
                          // enclose a cell that leads both.
                          topOfRow
                            ? 'border-t-2 border-b-2 border-t-ink border-b-ink'
                            : 'border-t-hairline-strong border-b-hairline-strong',
                          topOfColumn
                            ? 'border-l-2 border-r-2 border-l-ink border-r-ink'
                            : 'border-l-hairline-strong border-r-hairline-strong',
                        )
                        const shown = ranked ? times(cell.multiplier) : quarter.label
                        return (
                          <td key={id}>
                            {openable ? (
                              <button
                                type="button"
                                title={`${reading} · open these Data Fields`}
                                className={cn(face, 'cursor-pointer hover:brightness-115')}
                                onClick={() =>
                                  openPyramid(category.id, column.region, column.delay)
                                }
                              >
                                {shown}
                              </button>
                            ) : (
                              <span
                                title={`${reading} · not downloaded, sync this market to browse its Data Fields`}
                                className={face}
                              >
                                {shown}
                              </span>
                            )}
                          </td>
                        )
                      })}
                      {totalled && (
                        <td className={TOTAL_CELL} title={`${category.name}: ${TOTAL_NOTE}`}>
                          {fmt.int(rowTotals.get(category.id) ?? 0)}
                        </td>
                      )}
                    </tr>
                  ))}
                </tbody>
                {totalled && (
                  <tfoot>
                    <tr>
                      <th
                        scope="row"
                        title={TOTAL_NOTE}
                        className="pr-3 text-left font-medium text-ink-subtle"
                      >
                        Total
                      </th>
                      {data.columns.map((column) => {
                        const id = `${column.region}-${column.delay}`
                        return (
                          <td
                            key={id}
                            className={TOTAL_CELL}
                            title={`${column.region} D${column.delay}: ${TOTAL_NOTE}`}
                          >
                            {fmt.int(columnTotals.get(id) ?? 0)}
                          </td>
                        )
                      })}
                      <td className={cn(TOTAL_CELL, 'text-ink')} title={TOTAL_NOTE}>
                        {fmt.int(total)}
                      </td>
                    </tr>
                  </tfoot>
                )}
              </table>
            </div>
          </div>
        )}
        {data && data.categories.length > 0 && (
          <Legend view={view} needed={needed} allTime={allTime} />
        )}
      </Panel>
    </Page>
  )
}
