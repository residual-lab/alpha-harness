/**
 * Every column an Alpha table can show, grouped as BRAIN's own column picker groups them. A
 * screen names the ones its Alphas have; the table offers those and nothing else.
 */

import { createContext, useContext } from 'react'
import {
  POWER_POOL_CORRELATION_CEILING,
  PROD_CORRELATION_CEILING,
  SELF_CORRELATION_CEILING,
} from '@/api/platform'
import { cn } from '@/lib/cn'
import { CORE_METRICS, CORE_ORDER, DASH, fmt } from '@/lib/format'
import { Badge, cutoffTone, figureTone, signTone, TEXT_TONE } from '@/ui/kit'
import type { Column } from '@/ui/table'
import type { AlphaTableRow, Investability } from './row'

export type Group = 'Summary' | 'Settings' | 'Performance' | 'Correlation'
export const GROUPS: Group[] = ['Summary', 'Settings', 'Performance', 'Correlation']

export interface Option {
  key: string
  label: string
  group: Group
  column: Column<AlphaTableRow>
  /** What the column sorts by; without one it does not sort. */
  value?: (row: AlphaTableRow) => number | string | null | undefined
  /** The backend's name for it, where the backend sorts. */
  serverKey?: string
}

export const INVESTABILITY: Record<Investability, string> = {
  max_trade: 'Max Trade',
  max_position: 'Max Position',
  none: 'None',
}

/** The columns After-Cost Mode works out again on PnL net of cost. */
export const NET_OF_COST = new Set([
  'sharpe',
  'fitness',
  'returns',
  'drawdown',
  'margin',
  'trainSharpe',
  'testSharpe',
  'kRatio',
  'incrementalSharpe',
  'selfCorrelation',
  'powerPoolCorrelation',
  'portfolioCorrelation',
  'maxPairwiseCorrelation',
])

/** The correlation columns, which Show Correlation as Angles turns into angles. */
const CORRELATIONS = new Set([
  'prodCorrelation',
  'selfCorrelation',
  'powerPoolCorrelation',
  'portfolioCorrelation',
  'maxPairwiseCorrelation',
])

/** The angle whose cosine is ``rho``, in degrees: 0° moves together, 90° unrelated. */
const angleOf = (rho: number) => (Math.acos(Math.min(1, Math.max(-1, rho))) * 180) / Math.PI

/** Whether the table around a cell shows correlations as angles. */
export const AnglesContext = createContext(false)

/** A correlation as the table shows it: four decimals, or its angle in degrees. */
export function Correlation({ value }: { value: number | null | undefined }) {
  const angles = useContext(AnglesContext)
  if (value == null) return DASH
  return angles ? `${angleOf(value).toFixed(2)}°` : fmt.ratio(value, 4)
}

/** (Long − Short) ÷ (Long + Short) in stocks held, or null without both counts. */
export function imbalanceOf(r: AlphaTableRow): number | null {
  const { longCount: long, shortCount: short } = r
  if (long == null || short == null || long + short === 0) return null
  return (long - short) / (long + short)
}

/** A sort value, with correlations read as angles where the table shows them so. */
export function sortValue(option: Option, row: AlphaTableRow, angles: boolean) {
  const value = option.value?.(row) ?? null
  return angles && CORRELATIONS.has(option.key) && typeof value === 'number'
    ? angleOf(value)
    : value
}

const date = (iso: string | null | undefined) => (iso ? Date.parse(iso) : null)
const text = (value: string | null | undefined) => value || DASH
const list = (values: string[] | undefined) => {
  const joined = (values ?? []).join(', ')
  return (
    <span className="truncate text-body-compact text-ink-muted" title={joined}>
      {joined || DASH}
    </span>
  )
}
const tip = (label: string, title: string) => <span title={title}>{label}</span>

type Spec = Omit<Column<AlphaTableRow>, 'key' | 'header'> & {
  header?: Column<AlphaTableRow>['header']
  value?: Option['value']
  serverKey?: string
}

export const option = (group: Group, key: string, label: string, spec: Spec): Option => {
  const { value, serverKey, ...column } = spec
  return {
    key,
    label,
    group,
    column: { key, header: label, sortable: value !== undefined, ...column },
    ...(value ? { value } : {}),
    ...(serverKey ? { serverKey } : {}),
  }
}

type MetricKey = (typeof CORE_ORDER)[number]

const metric = (key: MetricKey) =>
  option('Performance', key, CORE_METRICS[key].label, {
    align: 'right',
    // "39.23 bps" is the widest figure; at the shared width it wrapped onto two lines.
    width: key === 'margin' ? 'minmax(100px,1fr)' : 'minmax(84px,1fr)',
    value: (r) => r[key],
    serverKey: key,
    cell: (r) => (
      <span className={cn('num', TEXT_TONE[figureTone(key, r[key], r.limits)])}>
        {CORE_METRICS[key].show(r[key])}
      </span>
    ),
  })

/** A ratio column, judged against the cutoff of the core figure it is a version of (`like`),
 *  or, with none, only called out when negative. */
const ratio = (
  key: keyof AlphaTableRow,
  label: string,
  title?: string,
  like?: 'sharpe' | 'fitness',
) =>
  option('Performance', key, label, {
    ...(title ? { header: tip(label, title) } : {}),
    width: 'minmax(84px,1fr)',
    align: 'right',
    value: (r) => r[key] as number | null | undefined,
    cell: (r) => {
      const v = r[key] as number | null | undefined
      const tone = like ? figureTone(like, v, r.limits) : cutoffTone(v)
      return <span className={cn('num', TEXT_TONE[tone])}>{fmt.ratio(v)}</span>
    },
  })

/** A correlation, green below the ceiling BRAIN holds it to and amber at or above. */
const correlation = (key: keyof AlphaTableRow, label: string, title: string, ceiling?: number) =>
  option('Correlation', key, label, {
    header: tip(label, title),
    width: 'minmax(112px,1fr)',
    align: 'right',
    value: (r) => r[key] as number | null | undefined,
    cell: (r) => {
      const v = r[key] as number | null | undefined
      if (v == null) return DASH
      return (
        <span
          className={cn(
            'num',
            ceiling === undefined
              ? undefined
              : v >= ceiling
                ? 'text-status-warning'
                : 'text-pnl-positive-text',
          )}
        >
          <Correlation value={v} />
        </span>
      )
    },
  })

/** Marks an Alpha simulated in Quick mode: real figures, but not one BRAIN will take. */
function QuickBadge({ row }: { row: AlphaTableRow }) {
  if (!row.quick) return null
  return (
    <Badge
      tone="outline"
      title={
        (row.refusedBy ?? []).length === 0
          ? 'Simulated in Quick mode. It passed every check BRAIN ran, so it is being simulated again in full, the only mode BRAIN takes for submission.'
          : 'Simulated in Quick mode, which runs only the checks that score an Alpha.'
      }
    >
      Quick
    </Badge>
  )
}

/** Every column, in the order a table shows them. */
export const OPTIONS: Option[] = [
  option('Summary', 'alphaId', 'Alpha', {
    width: '104px',
    value: (r) => r.alphaId,
    cell: (r) => <span className="num text-ink">{r.alphaId ?? DASH}</span>,
  }),
  option('Summary', 'trial', 'Trial', {
    width: '72px',
    value: (r) => r.trial,
    cell: (r) => (
      <span className="flex items-center gap-1.5">
        <span className="num">{r.trial ?? DASH}</span>
        <QuickBadge row={r} />
      </span>
    ),
  }),
  option('Summary', 'expression', 'Expression', {
    // Most of any spare width: on a wide screen the whole formula, not gaps between figures.
    width: 'minmax(240px,8fr)',
    cell: (r) => (
      <span className="flex min-w-0 items-center gap-1.5">
        <QuickBadge row={r} />
        <span className="num block truncate text-ink" title={r.expression ?? undefined}>
          {text(r.expression)}
        </span>
      </span>
    ),
  }),
  // What the task searches for, where that is not Sharpe; the screen names it.
  option('Summary', 'value', 'Objective', {
    width: '112px',
    align: 'right',
    value: (r) => r.value,
    cell: (r) => (
      <span className={cn('num', TEXT_TONE[cutoffTone(r.value)])}>{fmt.ratio(r.value)}</span>
    ),
  }),
  option('Summary', 'name', 'Name', {
    width: 'minmax(112px,1.5fr)',
    value: (r) => r.name,
    cell: (r) => text(r.name),
  }),
  option('Summary', 'dateCreated', 'Date Created', {
    width: '112px',
    value: (r) => date(r.dateCreated),
    serverKey: 'date_created',
    cell: (r) => <span className="num">{fmt.platformDay(r.dateCreated)}</span>,
  }),
  option('Summary', 'dateSubmitted', 'Date Submitted', {
    width: '112px',
    value: (r) => date(r.dateSubmitted),
    serverKey: 'date_submitted',
    cell: (r) => <span className="num">{fmt.platformDay(r.dateSubmitted)}</span>,
  }),
  option('Summary', 'classifications', 'Classifications', {
    width: 'minmax(112px,2fr)',
    cell: (r) => list(r.classifications),
  }),
  option('Summary', 'tags', 'Tags', { width: 'minmax(96px,1fr)', cell: (r) => list(r.tags) }),
  option('Summary', 'pyramids', 'Pyramids', {
    width: 'minmax(112px,1.5fr)',
    cell: (r) => list(r.pyramids),
  }),
  option('Summary', 'categories', 'Categories', {
    width: 'minmax(96px,1fr)',
    cell: (r) => list(r.categories),
  }),
  option('Summary', 'operatorCount', 'Operators', {
    width: '88px',
    align: 'right',
    value: (r) => r.operatorCount,
    serverKey: 'operator_count',
    cell: (r) => <span className="num">{r.operatorCount ?? DASH}</span>,
  }),
  option('Summary', 'failedChecks', 'Checks Failed', {
    width: 'minmax(140px,1.6fr)',
    value: (r) => r.failedChecks?.length,
    cell: (r) => {
      const failed = r.failedChecks ?? []
      if (failed.length === 0) return r.quick ? <QuickBadge row={r} /> : DASH
      return (
        <span className="truncate" title={failed.join(', ')}>
          {failed.map((name, i) => (
            <span
              key={name}
              // A check that failed without refusing the Alpha is shown, because it did fail,
              // but not in the colour that means "this is why you cannot submit".
              className={
                (r.refusedBy ?? []).includes(name) ? 'text-pnl-negative-text' : 'text-ink-subtle'
              }
            >
              {i > 0 && ', '}
              {name}
            </span>
          ))}
        </span>
      )
    },
  }),
  option('Summary', 'taskName', 'Task', {
    width: 'minmax(180px,1.4fr)',
    value: (r) => r.taskName,
    cell: (r) => <span className="truncate text-ink-muted">{text(r.taskName)}</span>,
  }),
  option('Summary', 'hasPnl', 'Daily PnL', {
    width: '88px',
    cell: (r) => (r.hasPnl ? <Badge tone="muted">stored</Badge> : DASH),
  }),
  option('Settings', 'region', 'Region', {
    width: '84px',
    value: (r) => r.region,
    cell: (r) => (
      <span className="num">
        {text(r.region)}
        {/* A region-agnostic Alpha's figures are one region's: name it. */}
        {r.standingRegion && <span className="text-ink"> → {r.standingRegion}</span>}
      </span>
    ),
  }),
  option('Settings', 'delay', 'Delay', {
    width: '64px',
    value: (r) => r.delay,
    cell: (r) => <span className="num">{r.delay == null ? DASH : `D${r.delay}`}</span>,
  }),
  option('Settings', 'universe', 'Universe', {
    width: '104px',
    value: (r) => r.universe,
    cell: (r) => <span className="num">{text(r.universe)}</span>,
  }),
  option('Settings', 'investability', 'Investability', {
    width: '104px',
    value: (r) => (r.investability ? INVESTABILITY[r.investability] : null),
    cell: (r) => (r.investability ? INVESTABILITY[r.investability] : DASH),
  }),
  option('Settings', 'neutralization', 'Neutralization', {
    width: 'minmax(112px,1fr)',
    value: (r) => r.neutralization,
    cell: (r) => text(r.neutralization),
  }),
  option('Settings', 'decay', 'Decay', {
    width: '72px',
    align: 'right',
    value: (r) => r.decay,
    cell: (r) => <span className="num">{r.decay ?? DASH}</span>,
  }),
  option('Settings', 'truncation', 'Truncation', {
    width: '96px',
    align: 'right',
    value: (r) => r.truncation,
    cell: (r) => <span className="num">{r.truncation ?? DASH}</span>,
  }),
  ...CORE_ORDER.map(metric),
  ratio('trainSharpe', 'Train Sharpe', undefined, 'sharpe'),
  // The held-out years, where an Alpha that only fits its train years shows its decay.
  ratio('testSharpe', 'Test Sharpe', undefined, 'sharpe'),
  ratio('testFitness', 'Test Fitness', undefined, 'fitness'),
  ratio(
    'afterCostSharpe',
    'After-Cost Sharpe (5 bps)',
    "The Sharpe after 5 bps of trading cost, charged on each day's own turnover. Empty until its daily PnL is downloaded.",
    'sharpe',
  ),
  ratio(
    'kRatio',
    '*K-Ratio',
    "Kestner's K-ratio, 2013 form: the slope of a line fitted to cumulative in-sample PnL, over its standard error times the number of days, times √250. Rewards steady growth.",
  ),
  option('Performance', 'incrementalSharpe', '*Incremental Sharpe', {
    header: tip(
      '*Incremental Sharpe',
      'Sharpe of the equal-weighted Portfolio of every submitted Alpha, less its Sharpe without this one: what this Alpha adds',
    ),
    width: 'minmax(104px,1fr)',
    align: 'right',
    value: (r) => r.incrementalSharpe,
    cell: (r) =>
      r.incrementalSharpe == null ? (
        DASH
      ) : Math.abs(r.incrementalSharpe) < 0.00005 ? (
        <span className="num">{fmt.ratio(0, 4)}</span>
      ) : (
        <span className={cn('num', TEXT_TONE[signTone(r.incrementalSharpe)])}>
          {r.incrementalSharpe > 0 ? '+' : ''}
          {fmt.ratio(r.incrementalSharpe, 4)}
        </span>
      ),
  }),
  option('Performance', 'longCount', 'Long Count', {
    width: 'minmax(84px,1fr)',
    align: 'right',
    value: (r) => r.longCount,
    cell: (r) => <span className="num">{r.longCount == null ? DASH : fmt.int(r.longCount)}</span>,
  }),
  option('Performance', 'shortCount', 'Short Count', {
    width: 'minmax(84px,1fr)',
    align: 'right',
    value: (r) => r.shortCount,
    cell: (r) => <span className="num">{r.shortCount == null ? DASH : fmt.int(r.shortCount)}</span>,
  }),
  option('Performance', 'imbalance', '*Long/Short Imbalance', {
    header: tip(
      '*Long/Short Imbalance',
      '(Long − Short) ÷ (Long + Short), in stocks held. 0% is balanced; +20% holds more stocks long, −20% more short.',
    ),
    width: 'minmax(112px,1fr)',
    align: 'right',
    value: imbalanceOf,
    cell: (r) => {
      const share = imbalanceOf(r)
      if (share === null) return DASH
      const imbalance = Math.round(share * 100)
      return (
        <span
          className="num"
          title={`${fmt.int(r.longCount ?? 0)} long · ${fmt.int(r.shortCount ?? 0)} short`}
        >
          {imbalance > 0 ? '+' : ''}
          {imbalance}%
        </span>
      )
    },
  }),
  correlation(
    'prodCorrelation',
    'Production Correlation',
    "BRAIN's Production Correlation for the Alpha. BRAIN wants it below 0.7, or a Sharpe 10% above each production Alpha it exceeds.",
    PROD_CORRELATION_CEILING,
  ),
  correlation(
    'selfCorrelation',
    '*Self-Correlation',
    "Highest daily PnL correlation with the other submitted Alphas of its region, Power Pool ones aside, over the last 4 years: BRAIN's Self-Correlation as its Alpha page shows it today",
    SELF_CORRELATION_CEILING,
  ),
  correlation(
    'powerPoolCorrelation',
    '*Power Pool Correlation',
    "Highest daily PnL correlation with the submitted Power Pool Alphas of its region, over the last 4 years: BRAIN's Power Pool Correlation as its Alpha page shows it today",
    POWER_POOL_CORRELATION_CEILING,
  ),
  correlation(
    'portfolioCorrelation',
    '*Portfolio Correlation',
    "Correlation of the Alpha's daily PnL with the equal-weighted Portfolio of every other submitted Alpha, over the last 4 years",
  ),
  option('Correlation', 'maxPairwiseCorrelation', '*Max Pairwise Correlation', {
    header: tip(
      '*Max Pairwise Correlation',
      'Highest daily PnL correlation with any one other submitted Alpha, over the last 4 years',
    ),
    width: 'minmax(112px,1fr)',
    align: 'right',
    value: (r) => r.maxPairwiseCorrelation,
    cell: (r) =>
      r.maxPairwiseCorrelation == null ? (
        DASH
      ) : (
        <span
          className={cn(
            'num',
            r.maxPairwiseCorrelation >= SELF_CORRELATION_CEILING
              ? 'text-pnl-negative-text'
              : r.maxPairwiseCorrelation >= POWER_POOL_CORRELATION_CEILING
                ? 'text-status-warning'
                : 'text-pnl-positive-text',
          )}
        >
          <Correlation value={r.maxPairwiseCorrelation} />
        </span>
      ),
  }),
]
