/**
 * Alphas combined at equal weight, as one portfolio: performance by period before and after
 * cost, year by year, and how the Alphas correlate. Shared by the Portfolio screen and the
 * Alphas screen's tag groups.
 */

import { Fragment } from 'react'
import { cn } from '@/lib/cn'
import { CORE_METRICS, CORE_ORDER, fmt } from '@/lib/format'
import { CostInput } from '@/screens/alpha-table'
import { figureTone, Panel, signTone, TEXT_TONE } from '@/ui/kit'
import { type Column, DataTable } from '@/ui/table'
import type { PortfolioResult, PortfolioStats, Windows } from './api'
import { ChartLegend, PortfolioChart } from './chart'
import { CorrelationMatrix } from './correlation'

export function PortfolioResults({
  result,
  stale,
  view,
  onOpen,
}: {
  result: PortfolioResult
  /** Newer figures are on their way: the old ones dim rather than read as current. */
  stale: boolean
  /** Where the cost charged is kept. */
  view: string
  onOpen: (alphaId: string, ids?: string[]) => void
}) {
  return (
    <div className={cn('flex flex-col gap-4 transition-opacity', stale && 'opacity-50')}>
      <Panel
        title="Combined In-Sample Performance"
        actions={<ChartLegend testStart={result.testStart} />}
        bodyClassName="flex flex-col gap-4"
      >
        <StatsTable result={result} windows={result.stats} />
        <PortfolioChart
          dates={result.dates}
          curve={result.curve}
          testStart={result.testStart}
          label="Cumulative PnL and drawdown of the combined Alphas"
        />
      </Panel>

      <Panel
        title="Estimated After-Cost Performance"
        actions={
          <span className="flex items-center gap-2 text-body text-ink-muted">
            Cost
            <CostInput view={view} />
            bps
          </span>
        }
        bodyClassName="flex flex-col gap-4"
      >
        <StatsTable result={result} windows={result.afterCost} />
        <PortfolioChart
          dates={result.dates}
          curve={result.afterCostCurve}
          testStart={result.testStart}
          label="Cumulative after-cost PnL and drawdown of the combined Alphas"
        />
      </Panel>

      <Panel title="Yearly">
        <DataTable
          label="Yearly stats of the combined Alphas"
          rows={result.yearly}
          rowKey={(r) => String(r.year)}
          columns={YEARLY_COLUMNS}
          maxHeight="none"
        />
      </Panel>

      {result.alphas > 1 && (
        <Panel
          title="Correlation"
          description={
            <>
              Pearson Correlation of Daily PnL over the last 4 years.
              <br />
              Each square is a pair of Alphas, in submission order: newest at the top left, oldest
              at the bottom right. Hover a square for the pair.
            </>
          }
          actions={
            <div className="grid grid-cols-[auto_auto_auto] items-baseline gap-x-2 gap-y-0.5 text-body text-ink-muted">
              {(
                [
                  ['Highest', result.highest],
                  ['Lowest', result.lowest],
                ] as const
              ).map(
                ([label, pair]) =>
                  pair && (
                    <Fragment key={label}>
                      <span>{label}</span>
                      <span className="num text-right text-ink">
                        {fmt.ratio(pair.correlation, 4)}
                      </span>
                      <span className="mono-metric text-ink-subtle">
                        {pair.a} · {pair.b}
                      </span>
                    </Fragment>
                  ),
              )}
            </div>
          }
        >
          <CorrelationMatrix result={result} onOpen={onOpen} />
        </Panel>
      )}
    </div>
  )
}

function metricColumns<T>(
  get: (row: T) => Partial<Record<(typeof CORE_ORDER)[number], number | null>> | null,
) {
  return CORE_ORDER.map((key): Column<T> => {
    const m = CORE_METRICS[key]
    return {
      key,
      header: m.label,
      align: 'right',
      // "39.23 bps" is the widest figure; at the shared width it wrapped onto two lines.
      width: key === 'margin' ? 'minmax(100px,1fr)' : 'minmax(84px,1fr)',
      cell: (row) => {
        const value = get(row)?.[key]
        return <span className={cn('num', TEXT_TONE[figureTone(key, value)])}>{m.show(value)}</span>
      },
    }
  })
}

const WINDOWS: { key: keyof Windows; label: string }[] = [
  { key: 'inSample', label: 'In Sample' },
  { key: 'train', label: 'Train Period' },
  { key: 'test', label: 'Test Period' },
]

interface WindowRow {
  label: string
  span: string
  stats: PortfolioStats | null
}

const STATS_COLUMNS: Column<WindowRow>[] = [
  {
    key: 'period',
    header: 'Period',
    width: 'minmax(300px,2fr)',
    cell: (r) => (
      <span className="flex min-w-0 items-baseline gap-3">
        <span className="text-ink">{r.label}</span>
        <span className="num truncate text-body-compact text-ink-subtle">{r.span}</span>
      </span>
    ),
  },
  ...metricColumns<WindowRow>((r) => r.stats),
]

function StatsTable({ result, windows }: { result: PortfolioResult; windows: Windows | null }) {
  const rows = WINDOWS.flatMap((w): WindowRow[] => {
    const period = result.periods?.[w.key]
    if (!period) return []
    return [
      {
        label: w.label,
        span: `${fmt.date(period.start)} – ${fmt.date(period.end)}`,
        stats: windows?.[w.key] ?? null,
      },
    ]
  })
  return (
    <DataTable
      label="Performance by period"
      rows={rows}
      rowKey={(r) => r.label}
      columns={STATS_COLUMNS}
      maxHeight="none"
    />
  )
}

type YearRow = PortfolioResult['yearly'][number]

const YEARLY_COLUMNS: Column<YearRow>[] = [
  {
    key: 'year',
    header: 'Year',
    width: '80px',
    cell: (r) => <span className="num">{r.year}</span>,
  },
  ...metricColumns<YearRow>((r) => r),
  {
    key: 'pnl',
    header: 'PnL',
    align: 'right',
    width: 'minmax(110px,1fr)',
    cell: (r) => <span className={cn('num', TEXT_TONE[signTone(r.pnl)])}>{fmt.int(r.pnl)}</span>,
  },
]
