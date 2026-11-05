/**
 * The Alpha page's sections, each showing what BRAIN's own page shows. Correlations and the
 * performance comparison are BRAIN jobs: a kept answer shows for free, only a click spends.
 */

import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { ChevronDownIcon, InfoIcon, MinusIcon, RefreshCwIcon, TriangleIcon } from 'lucide-react'
import { type ReactNode, useState } from 'react'
import { ApiError } from '@/api/http'
import type { AlphaCheck } from '@/api/types'
import { cn } from '@/lib/cn'
import { isNum } from '@/lib/format'
import {
  Badge,
  Button,
  Disclosure,
  ErrorNotice,
  LINK,
  Notice,
  Panel,
  Segmented,
  Skeleton,
  TEXT_TONE,
  type Tone,
} from '@/ui/kit'
import { Tooltip } from '@/ui/overlay'
import {
  type AlphaInfo,
  alpha as api,
  type CorrelationKind,
  type PeriodBlock,
  type Read,
  type Recordset,
} from './api'
import {
  brainValue,
  byResult,
  type Column,
  cell,
  checkMessage,
  firstPeriod,
  isPowerPool,
  METRICS,
  message,
  OS_TABLE,
  type Part,
  type Period,
  periodTabs,
  platformDay,
  stageOf,
  stamp,
  type Tab,
  YEARLY,
} from './brain'
import { PairChart } from './chart'

// ── Shared parts ───────────────────────────────────────────────────────────────────────────

/** BRAIN's explanation of a figure, under BRAIN's heading for it, behind a dot. */
export function Tip({
  title,
  label = title,
  children,
}: {
  title: string
  /** Names the dot for a screen reader where the heading alone repeats. */
  label?: string
  children: ReactNode
}) {
  return (
    <Tooltip
      content={
        <span className="flex flex-col gap-1">
          <span className="font-medium">{title}</span>
          <span>{children}</span>
        </span>
      }
    >
      <button
        type="button"
        aria-label={`About ${label}`}
        className="inline-flex size-4 shrink-0 items-center justify-center rounded-full text-ink-tertiary transition-colors hover:text-ink"
      >
        <InfoIcon className="size-3" aria-hidden />
      </button>
    </Tooltip>
  )
}

/** BRAIN's words, its bold figures kept bold. */
export function Sentence({ parts }: { parts: Part[] }) {
  return parts.map((part, i) =>
    part.href ? (
      <a key={i} href={part.href} target="_blank" rel="noopener noreferrer" className={LINK}>
        {part.text}
      </a>
    ) : part.bold ? (
      <strong key={i} className="num font-semibold text-ink">
        {part.text}
      </strong>
    ) : (
      <span key={i}>{part.text}</span>
    ),
  )
}

function PeriodTabs({
  tabs,
  value,
  onChange,
}: {
  tabs: Tab[]
  value: Period
  onChange: (period: Period) => void
}) {
  return (
    <div className="flex items-center gap-2">
      <span className="text-body-compact text-ink-subtle">Period</span>
      <Segmented
        label="Period"
        value={value}
        onChange={onChange}
        items={tabs.map((t) => ({ value: t.period, label: t.label, disabled: !t.available }))}
      />
    </div>
  )
}

/**
 * BRAIN's six figures, sized to the section rather than the screen: six across when they fit,
 * else three and three. The section's own width decides, as the page may set it in a column.
 */
export const FIGURE_GRID = 'grid grid-cols-3 gap-x-3 gap-y-3 @2xl:grid-cols-6'

/** BRAIN's six figures in a row, under the label it gives the row. */
function Figures({
  label,
  note,
  block,
}: {
  label: string
  note?: string
  block: Record<string, unknown> | undefined
}) {
  return (
    <div className="@container">
      <div className="flex flex-col gap-3 @4xl:flex-row @4xl:items-end @4xl:gap-6">
        <div className="flex min-w-28 flex-col gap-0.5">
          <span className="text-body-compact font-medium text-ink">{label}</span>
          {note && <span className="num text-body-compact text-ink-subtle">{note}</span>}
        </div>
        <dl className={cn(FIGURE_GRID, 'flex-1')}>
          {METRICS.map((m) => {
            const value = block?.[m.id]
            return (
              <div key={m.id} className="flex min-w-0 flex-col gap-1">
                <dt className="flex items-center gap-1 text-caption text-ink-subtle">
                  {m.title}
                  <Tip title={m.title}>{m.tip}</Tip>
                </dt>
                <dd className="num text-title whitespace-nowrap text-ink">
                  {isNum(value) ? brainValue(value, m.type) : ''}
                </dd>
              </div>
            )
          })}
        </dl>
      </div>
    </div>
  )
}

/** BRAIN colours a yearly row by the period it fell in. */
const STAGE_EDGE: Record<string, string> = {
  TRAIN: 'border-l-status-running',
  TEST: 'border-l-status-warning',
  OS: 'border-l-pnl-positive',
}

function Table({ columns, rows }: { columns: Column[]; rows: Record<string, unknown>[] }) {
  return (
    <div className="overflow-x-auto rounded-md border border-hairline">
      <table className="w-full text-body-compact whitespace-nowrap">
        <thead>
          <tr className="border-b border-hairline bg-surface-2 text-caption text-ink-subtle">
            {columns.map((c) => (
              <th
                key={c.id}
                className={cn(
                  'px-2 py-1.5 font-medium',
                  c.type === 'year' ? 'text-left' : 'text-right',
                )}
              >
                {c.title}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} className="border-b border-hairline-subtle last:border-b-0">
              {columns.map((c, j) => (
                <td
                  key={c.id}
                  className={cn(
                    'num px-2 py-1.5',
                    c.type === 'year' ? 'text-left text-ink-muted' : 'text-right text-ink',
                    j === 0 && 'border-l-2 border-l-transparent',
                    j === 0 && STAGE_EDGE[String(row['stage'])],
                  )}
                >
                  {cell(row[c.id], c.type)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** A recordset's rows as objects, keyed by its column names. */
const rowsOf = (set: Recordset | null | undefined) => {
  const names = (set?.schema?.properties ?? []).map((p) => p.name)
  return (set?.records ?? []).map((r) => Object.fromEntries(names.map((n, i) => [n, r[i]])))
}

// ── Testing Status ─────────────────────────────────────────────────────────────────────────

const RESULT_TONE: Record<string, Tone> = {
  PASS: 'profit',
  FAIL: 'loss',
  ERROR: 'loss',
  WARNING: 'warn',
  PENDING: 'muted',
}

const RESULT_DOT: Record<string, string> = {
  PASS: 'bg-pnl-positive',
  FAIL: 'bg-pnl-negative',
  ERROR: 'bg-pnl-negative',
  WARNING: 'bg-status-warning',
  PENDING: 'bg-ink-tertiary',
}

export function CheckList({ checks, stage }: { checks: AlphaCheck[]; stage: 'IS' | 'OS' }) {
  return (
    <ul className="flex flex-col gap-1.5">
      {checks.map((check, i) => (
        <li key={`${check.name}-${i}`} className="flex gap-2 text-body text-ink-muted">
          <span
            aria-hidden
            className={cn(
              'mt-2 size-1.5 shrink-0 rounded-full',
              RESULT_DOT[check.result ?? 'PENDING'],
            )}
          />
          <span className="min-w-0 text-pretty break-words">
            <Sentence parts={checkMessage(check, stage)} />
          </span>
        </li>
      ))}
    </ul>
  )
}

export function TestingStatus({
  alpha,
  testControl,
  testShown,
}: {
  alpha: AlphaInfo
  testControl: boolean
  testShown: boolean
}) {
  const [period, setPeriod] = useState(() => firstPeriod(alpha.stage, testControl, testShown))
  const stage = stageOf(period)
  const checks = alpha.periods[stage]?.checks
  const quick =
    checks !== undefined &&
    alpha.status === 'UNSUBMITTED' &&
    alpha.settings['simulationMode'] === 'QUICK'
  return (
    <Panel
      title={`${stage.toUpperCase()} Testing Status`}
      actions={
        <PeriodTabs
          tabs={periodTabs(alpha.periods, testControl, testShown)}
          value={period}
          onChange={setPeriod}
        />
      }
      bodyClassName="flex flex-col gap-2"
    >
      {quick && (
        <Notice tone="info">
          {checks.some((c) => c.result === 'FAIL')
            ? 'This alpha did not pass all quick checks. Fix the failures below, then run Full Simulate.'
            : 'Quick Simulate is not enough — Full Simulate is needed to submit this alpha.'}
        </Notice>
      )}
      {byResult(checks ?? []).map(([result, group]) => (
        <Disclosure
          key={result}
          // Opened on arrival: these are why it cannot be submitted.
          defaultOpen={result === 'FAIL'}
          summary={
            <span className={cn('font-medium', TEXT_TONE[RESULT_TONE[result] ?? 'neutral'])}>
              {group.length} {result}
            </span>
          }
        >
          <CheckList checks={group} stage={stage === 'is' ? 'IS' : 'OS'} />
        </Disclosure>
      ))}
    </Panel>
  )
}

// ── Summary ────────────────────────────────────────────────────────────────────────────────

const words = (value: string) => value.charAt(0) + value.slice(1).toLowerCase().replaceAll('_', ' ')

function Match({ tip, about, children }: { tip: string; about: Part[]; children: ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1 rounded-xs border border-hairline-strong bg-surface-2 py-0.5 pr-1 pl-2 text-body-compact text-ink-muted">
      {children}
      <Tip title={tip}>
        <Sentence parts={about} />
      </Tip>
    </span>
  )
}

/** The grade, themes, classifications and pyramids BRAIN tags the Alpha with. */
function Matches({ alpha }: { alpha: AlphaInfo }) {
  const pyramids = alpha.pyramidThemes?.['pyramids']
  const effective = alpha.pyramidThemes?.['effective']
  const tagged = [
    ...(alpha.grade ? [<Badge key="grade">{words(alpha.grade)}</Badge>] : []),
    ...alpha.themes.map((t, i) => (
      <Match
        key={`theme-${i}`}
        tip="Matching theme"
        about={message('IS.MATCHES_THEMES.PASS.single', {
          name: t['name'],
          multiplier: String(t['multiplier']),
        })}
      >
        Theme: <strong className="text-ink">{String(t['name'])}</strong> x{String(t['multiplier'])}
      </Match>
    )),
    ...alpha.classifications.map((c) => (
      <Match
        key={c.id}
        tip="Matching classification"
        about={message('IS.MATCHES_CLASSIFICATION.PASS.single', { value: c.name })}
      >
        {c.name}
      </Match>
    )),
    ...(Array.isArray(pyramids) ? pyramids : []).map((p: Record<string, unknown>, i) => (
      <Match
        key={`pyramid-${i}`}
        tip="Matching pyramid themes"
        about={message('IS.MATCHES_PYRAMID.PASS.single', {
          name: p['name'],
          multiplier: String(p['multiplier']),
          effective,
        })}
      >
        Pyramid theme: <strong className="text-ink">{String(p['name'])}</strong> x
        {String(p['multiplier'])}
      </Match>
    )),
  ]
  return tagged.length > 0 && <div className="flex flex-wrap gap-2">{tagged}</div>
}

/** A block's own figures: OS dates its row, every other period calls it Aggregate Data. */
function Aggregate({ period, block }: { period: Period; block: Record<string, unknown> }) {
  return period === 'os' ? (
    <Figures
      label="Start Date"
      note={platformDay(String(block['startDate'] ?? ''))}
      block={block}
    />
  ) : (
    <Figures label="Aggregate Data" block={block} />
  )
}

function Yearly({
  alpha,
  period,
  block,
  trainOnly,
}: {
  alpha: AlphaInfo
  period: Period
  block: PeriodBlock
  /** While the test period hides, TRAIN shows only its own years. */
  trainOnly: boolean
}) {
  const yearly = useQuery({
    queryKey: ['alpha', alpha.alphaId, 'recordset', 'yearly-stats'],
    queryFn: () => api.recordset(alpha.alphaId, 'yearly-stats'),
    staleTime: Number.POSITIVE_INFINITY,
    retry: false,
  })
  if (yearly.isPending) return <Skeleton className="h-48" label="Loading the yearly figures" />
  if (yearly.isError)
    return <ErrorNotice error={yearly.error} title="The yearly figures did not load" />
  const rows = rowsOf(yearly.data).filter((r) => !trainOnly || r['stage'] === 'TRAIN')
  return (
    <div className="flex flex-col gap-3">
      {yearly.data.problem && <Notice tone="warn">{yearly.data.problem}</Notice>}
      {period === 'os' && <Table columns={OS_TABLE} rows={[block]} />}
      {rows.length > 0 ? (
        <Table columns={YEARLY} rows={rows} />
      ) : (
        <p className="text-body text-ink-subtle">
          Yearly summary not available, please clone this alpha and re-simulate.
        </p>
      )}
    </div>
  )
}

function Subsection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-3 border-t border-hairline-subtle pt-4">
      <h3 className="text-body font-medium text-ink">{title}</h3>
      {children}
    </section>
  )
}

/** BRAIN's regions inside a global or region-agnostic Alpha, each with its own figures. */
const regionsOf = (block: PeriodBlock): [string, Record<string, unknown>][] => {
  const subregions = block['subregions']
  if (subregions && typeof subregions === 'object')
    return Object.entries(subregions as Record<string, Record<string, unknown>>)
  return (['Amer', 'Apac', 'Emea'] as const).flatMap((r) => {
    const found = block[`glb${r}`]
    return found && typeof found === 'object'
      ? [[r.toUpperCase(), found as Record<string, unknown>]]
      : []
  })
}

export function Summary({
  alpha,
  testControl,
  testShown,
}: {
  alpha: AlphaInfo
  testControl: boolean
  testShown: boolean
}) {
  const [period, setPeriod] = useState(() => firstPeriod(alpha.stage, testControl, testShown))
  const [yearsShown, setYearsShown] = useState(true)
  const block = alpha.periods[period]
  // A region-agnostic Alpha has no yearly figures of its own, only its regions'.
  const yearly = block !== undefined && block['subregions'] === undefined
  return (
    <Panel
      title={`${stageOf(period).toUpperCase()} Summary`}
      actions={
        <PeriodTabs
          tabs={periodTabs(alpha.periods, testControl, testShown)}
          value={period}
          onChange={setPeriod}
        />
      }
      bodyClassName="flex flex-col gap-4"
    >
      <Matches alpha={alpha} />
      {block === undefined ? (
        <p className="text-body text-ink-subtle">
          Summary not available, please clone this alpha and re-simulate.
        </p>
      ) : (
        <>
          <div className="flex items-end gap-2">
            <div className="min-w-0 flex-1">
              <Aggregate period={period} block={block} />
            </div>
            {yearly && (
              <Button
                size="icon-sm"
                variant="ghost"
                aria-expanded={yearsShown}
                aria-label={yearsShown ? 'Hide the yearly figures' : 'Show the yearly figures'}
                onClick={() => setYearsShown(!yearsShown)}
              >
                <ChevronDownIcon
                  className={cn('transition-transform', yearsShown && 'rotate-180')}
                />
              </Button>
            )}
          </div>
          {yearly && yearsShown && (
            <Yearly
              alpha={alpha}
              period={testControl ? 'is' : period}
              block={block}
              trainOnly={testControl && !testShown && period === 'train'}
            />
          )}
          {regionsOf(block).map(([region, figures]) => (
            <Subsection key={region} title={region}>
              <Aggregate period={period} block={figures} />
            </Subsection>
          ))}
          {block.riskNeutralized && (
            <Subsection title="Risk neutralized">
              <Aggregate period={period} block={block.riskNeutralized} />
            </Subsection>
          )}
          {block.investabilityConstrained && (
            <Subsection title="Investability constrained">
              <Aggregate period={period} block={block.investabilityConstrained} />
            </Subsection>
          )}
        </>
      )}
    </Panel>
  )
}

// ── Regional Child Alphas ──────────────────────────────────────────────────────────────────

const CHILD_METRICS = METRICS.map((m) =>
  m.id === 'drawdown' ? { ...m, title: 'Max Drawdown' } : m,
)

const plural = (count: number, one: string) => `${count} ${one}${count === 1 ? '' : 's'}`

function ChildAlpha({ alpha }: { alpha: AlphaInfo }) {
  const checks = alpha.periods.is?.checks ?? []
  const failed = checks.filter((c) => c.result === 'FAIL')
  const pending = checks.filter((c) => c.result === 'PENDING')
  const passed = checks.filter((c) => c.result === 'PASS').length
  const region = String(alpha.settings['region'] ?? '')
  const subregions = alpha.periods.is?.['subregions'] as Record<string, PeriodBlock> | undefined
  const figures = subregions?.[region] ?? alpha.periods.is
  const status = (
    <span className={cn('text-body-compact', failed.length ? TEXT_TONE.loss : TEXT_TONE.profit)}>
      {failed.length
        ? `Tests: ${failed.length} failed`
        : `Tests: ${passed} pass, ${pending.length} pending submission check`}
    </span>
  )
  return (
    <div className="@container flex flex-col gap-3 px-4 py-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-title text-ink">{region}</h3>
        <div className="flex flex-wrap items-center gap-3">
          {failed.length || pending.length ? (
            <Tooltip
              content={
                <span className="flex flex-col gap-2">
                  {failed.length > 0 && (
                    <span className="flex flex-col gap-1">
                      <span className="font-medium">{plural(failed.length, 'Failed Test')}</span>
                      <CheckList checks={failed} stage="IS" />
                    </span>
                  )}
                  {pending.length > 0 && (
                    <span className="flex flex-col gap-1">
                      <span className="font-medium">{plural(pending.length, 'Pending Test')}</span>
                      <CheckList checks={pending} stage="IS" />
                    </span>
                  )}
                </span>
              }
            >
              <button type="button">{status}</button>
            </Tooltip>
          ) : (
            status
          )}
          <Button
            size="sm"
            render={<Link to="/alpha/$alphaId" params={{ alphaId: alpha.alphaId }} />}
          >
            View alpha details
          </Button>
        </div>
      </div>
      <dl className={FIGURE_GRID}>
        {CHILD_METRICS.map((m) => {
          const value = figures?.[m.id]
          return (
            <div key={m.id} className="flex min-w-0 flex-col gap-1">
              <dt className="text-caption text-ink-subtle">{m.title}</dt>
              <dd className="num text-title whitespace-nowrap text-ink">
                {isNum(value) ? brainValue(value, m.type) : '-'}
              </dd>
            </div>
          )
        })}
      </dl>
    </div>
  )
}

/** A region-agnostic parent's Alphas, one per region, as BRAIN lists them. */
export function RegionalChildren({ alpha }: { alpha: AlphaInfo }) {
  const children = useQueries({
    queries: alpha.children.map((child) => ({
      queryKey: ['alpha', child, 'page'],
      queryFn: () => api.page(child),
      retry: false,
    })),
  })
  return (
    <Panel
      title="Regional Child Alphas"
      bodyClassName="flex flex-col divide-y divide-hairline-subtle p-0"
    >
      {children.length === 0 && (
        <p className="px-4 py-3 text-body text-ink-subtle">No child alphas found</p>
      )}
      {children.map((child, i) =>
        child.isPending ? (
          <Skeleton key={i} className="m-4 h-24" label="Loading a regional Alpha" />
        ) : child.isError ? (
          <ErrorNotice
            key={i}
            className="m-4"
            error={child.error}
            title="Error loading child alphas"
          />
        ) : (
          <ChildAlpha key={child.data.alpha.alphaId} alpha={child.data.alpha} />
        ),
      )}
    </Panel>
  )
}

// ── BRAIN jobs: run on a click, kept after ─────────────────────────────────────────────────

/**
 * Kept answers show on load for free; the refresh button is what spends the budget. One
 * query per job, keyed by what it holds, so a run replaces the kept answer in place.
 */
function useBudgeted<T extends { cached: boolean }>(
  key: unknown[],
  fetch: (mode: Read) => Promise<T>,
) {
  const queryClient = useQueryClient()
  const kept = useQuery({
    queryKey: key,
    queryFn: () => fetch('cached'),
    retry: false,
    staleTime: Number.POSITIVE_INFINITY,
  })
  const run = useMutation({
    meta: { inline: true },
    mutationFn: () => fetch(kept.data?.cached ? 'refresh' : 'run'),
    onSuccess: (data) => queryClient.setQueryData(key, data),
  })
  return { kept, run }
}

/** Last Run, the refresh that runs it again, and the arrow that opens the answer. */
function RunBar({
  what,
  lastRun,
  running,
  disabled,
  open,
  onRun,
  onToggle,
}: {
  what: string
  lastRun: string | undefined
  running: boolean
  disabled: boolean
  open: boolean
  onRun: () => void
  onToggle: () => void
}) {
  return (
    <div className="flex items-center justify-end gap-1">
      <span className="num text-caption whitespace-nowrap text-ink-subtle">
        Last Run: {lastRun ? stamp(lastRun) : '-'}
      </span>
      <Button
        size="icon-sm"
        variant="ghost"
        aria-label={`Run ${what} on BRAIN`}
        title={`Run ${what} on BRAIN`}
        loading={running}
        disabled={disabled}
        onClick={onRun}
      >
        {!running && <RefreshCwIcon />}
      </Button>
      <Button
        size="icon-sm"
        variant="ghost"
        aria-expanded={open}
        aria-label={open ? `Hide ${what}` : `Show ${what}`}
        onClick={onToggle}
      >
        <ChevronDownIcon className={cn('transition-transform', open && 'rotate-180')} />
      </Button>
    </div>
  )
}

// ── Correlation ────────────────────────────────────────────────────────────────────────────

/** Each correlation's heading in BRAIN's explanation, and whom it compares the Alpha with. */
const COMPARED_WITH = {
  self: ['Self Correlation', 'all other alphas submitted by the same user'],
  'power-pool': [
    'Power Pool Correlation',
    'all other power pool alphas submitted by the same user',
  ],
  prod: ['Prod correlation', 'all other alphas submitted by all WorldQuant BRAIN users'],
} as const

const CORRELATIONS: { kind: CorrelationKind; title: string; none: string }[] = [
  { kind: 'self', title: 'Self Correlation', none: 'There are no self-correlated alphas.' },
  {
    kind: 'power-pool',
    title: 'Power Pool Correlation',
    none: 'There are no self-correlated alphas.',
  },
  { kind: 'prod', title: 'Prod Correlation', none: 'There are no prod-correlated alphas.' },
]

const FOUR = new Intl.NumberFormat('en-US', { minimumFractionDigits: 4, maximumFractionDigits: 4 })
const TWO = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

function Extreme({
  label,
  kind,
  value,
}: {
  label: 'Maximum' | 'Minimum'
  kind: CorrelationKind
  value: unknown
}) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="flex items-center gap-1 text-caption text-ink-subtle">
        {label}
        <Tip title={COMPARED_WITH[kind][0]} label={`${label} ${COMPARED_WITH[kind][0]}`}>
          {`${label} Pearson correlation coefficient from comparing a given alpha to ${COMPARED_WITH[kind][1]}`}
        </Tip>
      </span>
      <span className="num text-title text-ink">{isNum(value) ? FOUR.format(value) : '-'}</span>
    </div>
  )
}

/** The columns BRAIN shows of each correlated Alpha, in the order the answer lists them. */
const SHOWN = new Set([
  'name',
  'universe',
  'correlation',
  'sharpe',
  'turnover',
  'fitness',
  'returns',
  'drawdown',
  'margin',
])

const figure = (type: string | undefined, name: string, value: unknown) => {
  const v = isNum(value) ? value : 0
  return type === 'percent' || type === 'permyriad'
    ? brainValue(v, type)
    : (name === 'correlation' ? FOUR : TWO).format(v)
}

function CorrelatedAlphas({
  alpha,
  data,
}: {
  alpha: AlphaInfo
  data: { schema?: Recordset['schema']; records?: unknown[][] }
}) {
  const properties = data.schema?.properties ?? []
  const columns = properties.flatMap((p, at) => (SHOWN.has(p.name) ? [{ ...p, at }] : []))
  const idAt = properties.findIndex((p) => p.name === 'id')
  const own = alpha.periods.is ?? {}
  return (
    <div className="max-h-80 overflow-auto rounded-md border border-hairline">
      <table className="w-full text-body-compact whitespace-nowrap">
        <thead className="sticky top-0 bg-surface-2">
          <tr className="text-ink-subtle">
            {columns.map((c) => (
              <th
                key={c.name}
                className={cn(
                  'px-2 py-1.5 font-medium',
                  c.type === 'string' ? 'text-left' : 'text-right',
                )}
              >
                {c.title ?? c.name}
              </th>
            ))}
          </tr>
          <tr className="border-b border-hairline bg-surface-3 text-ink">
            {columns.map((c) => (
              <td
                key={c.name}
                className={cn('num px-2 py-1.5', c.type !== 'string' && 'text-right')}
              >
                {c.name === 'name'
                  ? `${alpha.name ?? 'anonymous'} (current)`
                  : c.name === 'universe'
                    ? String(alpha.settings['universe'] ?? '')
                    : c.name === 'correlation'
                      ? TWO.format(1)
                      : figure(c.type, c.name, own[c.name])}
              </td>
            ))}
          </tr>
        </thead>
        <tbody>
          {(data.records ?? []).map((record, i) => (
            <tr key={i} className="border-b border-hairline-subtle last:border-b-0">
              {columns.map((c) => {
                const value = record[c.at]
                const id = record[idAt]
                return (
                  <td
                    key={c.name}
                    className={cn('num px-2 py-1.5', c.type !== 'string' && 'text-right')}
                  >
                    {c.name === 'name' && typeof id === 'string' ? (
                      <Link to="/alpha/$alphaId" params={{ alphaId: id }} className={LINK}>
                        {value ? String(value) : 'anonymous'}
                      </Link>
                    ) : c.type === 'string' ? (
                      String(value ?? '')
                    ) : (
                      figure(c.type, c.name, value)
                    )}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** Highcharts' log-axis labels: 1, 100, 10k, 1M, 100M. */
const decade = (n: number) => (n >= 1e6 ? `${n / 1e6}M` : n >= 1e3 ? `${n / 1e3}k` : String(n))
const ONE_DECIMAL = new Intl.NumberFormat('en-US', { minimumFractionDigits: 1 })

/** How many production Alphas fall in each band of correlation, on a log scale. */
function Histogram({ data }: { data: { schema?: Recordset['schema']; records?: unknown[][] } }) {
  const properties = data.schema?.properties ?? []
  const at = (name: string) => properties.findIndex((p) => p.name === name)
  const [low, high, count] = [at('min'), at('max'), at('alphas')]
  const bars = (data.records ?? []).map((r) => ({
    band: `${ONE_DECIMAL.format(Number(r[low]))}…${ONE_DECIMAL.format(Number(r[high]))}`,
    alphas: isNum(r[count]) ? r[count] : 0,
  }))
  const most = Math.max(1, ...bars.map((b) => b.alphas))
  // Ticks every two decades, as BRAIN's axis has them, up to the first above the tallest bar.
  const decades = Math.max(2, Math.ceil(Math.log10(most) / 2) * 2)
  const ticks = Array.from({ length: decades / 2 + 1 }, (_, i) => 10 ** (i * 2))
  const [width, height, left, bottom] = [600, 240, 52, 56]
  const plot = height - bottom - 8
  const y = (n: number) => height - bottom - (n <= 1 ? 0 : (Math.log10(n) / decades) * plot)
  const step = (width - left) / Math.max(1, bars.length)
  return (
    <figure className="flex flex-col gap-1">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="w-full text-ink-subtle"
        role="img"
        aria-label="Number of production Alphas in each band of correlation"
      >
        <text
          x={12}
          y={(height - bottom) / 2}
          transform={`rotate(-90 12 ${(height - bottom) / 2})`}
          textAnchor="middle"
          fontSize={10}
          className="fill-current"
        >
          Nº Prod Alphas
        </text>
        {ticks.map((t) => (
          <g key={t}>
            <line
              x1={left}
              x2={width}
              y1={y(t)}
              y2={y(t)}
              className="stroke-hairline"
              strokeWidth={1}
            />
            <text x={left - 6} y={y(t) + 3} textAnchor="end" fontSize={10} className="fill-current">
              {decade(t)}
            </text>
          </g>
        ))}
        {bars.map((b, i) => {
          const x = left + i * step
          // A band of one Alpha is a sliver rather than nothing, so it can still be seen.
          const top = b.alphas > 0 ? Math.min(y(b.alphas), height - bottom - 2) : height - bottom
          return (
            <g key={b.band}>
              {b.alphas > 0 && (
                <rect
                  x={x + step * 0.15}
                  y={top}
                  width={step * 0.7}
                  height={height - bottom - top}
                  className="fill-pnl-positive"
                >
                  <title>{`${b.band}\nAlphas: ${b.alphas.toLocaleString('en-US')}`}</title>
                </rect>
              )}
              <text
                x={x + step / 2}
                y={height - bottom + 10}
                transform={`rotate(-45 ${x + step / 2} ${height - bottom + 10})`}
                textAnchor="end"
                fontSize={9}
                className="fill-current"
              >
                {b.band}
              </text>
            </g>
          )
        })}
      </svg>
    </figure>
  )
}

const platformStatus = (error: unknown) =>
  error instanceof ApiError ? Number(error.body['platformStatus']) : null

function CorrelationRow({
  alpha,
  kind,
  title,
  none,
}: {
  alpha: AlphaInfo
  kind: CorrelationKind
  title: string
  none: string
}) {
  const { kept, run } = useBudgeted(['alpha', alpha.alphaId, 'correlation', kind], (mode) =>
    api.correlation(alpha.alphaId, kind, mode),
  )
  const [open, setOpen] = useState(false)
  const data = kept.data?.cached ? kept.data : null
  const error = run.error ?? kept.error
  const unanswered = [410, 412].includes(platformStatus(error) ?? 0)
  return (
    <div className="flex flex-col gap-3 py-3">
      <div className="grid grid-cols-2 items-end gap-x-4 gap-y-2 @2xl:grid-cols-[minmax(9rem,1fr)_6rem_6rem_auto]">
        <span className="col-span-2 self-center text-body font-medium text-ink @2xl:col-span-1">
          {title}
        </span>
        <Extreme label="Maximum" kind={kind} value={data?.max} />
        <Extreme label="Minimum" kind={kind} value={data?.min} />
        <div className="col-span-2 @2xl:col-span-1">
          <RunBar
            what={title}
            lastRun={data?.fetchedAt}
            running={run.isPending}
            disabled={kept.isPending}
            open={open}
            onRun={() => run.mutate()}
            onToggle={() => {
              // Opening an answer never asked for asks for it, as BRAIN's arrow does.
              if (!data && !run.isPending) run.mutate()
              setOpen(!open)
            }}
          />
        </div>
      </div>
      {error && !unanswered && <ErrorNotice error={error} title={`${title} did not run`} />}
      {/* BRAIN's own answer when the correlation does not apply, shown wherever it comes from. */}
      {unanswered && (
        <p className="text-body text-ink-subtle">
          {platformStatus(error) === 412 ? 'Invalid alpha PnL.' : none}
        </p>
      )}
      {open &&
        !unanswered &&
        (run.isPending && !data ? (
          <Skeleton className="h-40" label={`BRAIN is working out ${title}`} />
        ) : (
          data &&
          (!isNum(data.max) ? (
            <p className="text-body text-ink-subtle">{none}</p>
          ) : kind === 'prod' ? (
            <Histogram data={data} />
          ) : (
            <CorrelatedAlphas alpha={alpha} data={data} />
          ))
        ))}
    </div>
  )
}

export function Correlation({ alpha }: { alpha: AlphaInfo }) {
  return (
    <Panel
      title="Correlation"
      bodyClassName="@container flex flex-col divide-y divide-hairline-subtle py-0"
    >
      {CORRELATIONS.filter((c) => c.kind !== 'power-pool' || isPowerPool(alpha)).map((c) => (
        <CorrelationRow key={c.kind} alpha={alpha} {...c} />
      ))}
    </Panel>
  )
}

// ── Performance Comparison ─────────────────────────────────────────────────────────────────

/** BRAIN's competition scores, each before and after, when the answer carries them; with
 *  BRAIN's explanation of the before, after and change. */
const SCORES: { key: string; title: string; tips: [string, string, string] }[] = [
  {
    key: 'score',
    title: 'Score',
    tips: [
      'Your team’s current delay score.',
      'Expected team’s delay score after submitting this alpha.',
      'Change in your team’s delay score if this alpha is submitted.',
    ],
  },
  {
    key: 'datafieldsScore',
    title: 'Data Fields Score',
    tips: [
      'Your current data fields score.',
      'Expected data fields score after submitting this alpha.',
      'Change in your data fields score if this alpha is submitted.',
    ],
  },
  {
    key: 'weeklyScore',
    title: 'Weekly Score',
    tips: [
      'Your current weekly score.',
      'Expected weekly score after submitting this alpha.',
      'Change in your weekly score if this alpha is submitted.',
    ],
  },
  {
    key: 'robustnessScore',
    title: 'Robustness Score',
    tips: [
      'Your current robustness score.',
      'Expected robustness score after submitting this alpha.',
      'Change in your robustness score if this alpha is submitted.',
    ],
  },
]

const PARTITION: Record<string, string> = {
  REGULAR: 'Regular alpha',
  SUPER: 'Super alpha',
  EQUITY: 'Equity',
  '0': 'Delay-0',
  '1': 'Delay-1',
}

const COMPARED = YEARLY.slice(0, 7).map((c) => c.id)

const unit = (type: string | undefined) =>
  type === 'percent' ? ' %' : type === 'permyriad' ? ' bps' : ''

/** A figure as BRAIN's comparison prints it: scaled, with the unit set apart. */
const bare = (value: number, type: string | undefined) =>
  brainValue(value, type ?? 'decimal').replace(/%$| bps$/, '')

function Change({ value, type }: { value: number; type: string | undefined }) {
  const tone: Tone = value > 0 ? 'profit' : value < 0 ? 'loss' : 'muted'
  const Icon = value === 0 ? MinusIcon : TriangleIcon
  return (
    <span className={cn('inline-flex items-center gap-0.5', TEXT_TONE[tone])}>
      <Icon
        aria-hidden
        className={cn(
          'size-2.5',
          value > 0 && 'fill-current',
          value < 0 && 'rotate-180 fill-current',
        )}
      />
      {bare(value, type)}
    </span>
  )
}

function Score({
  title,
  tips,
  pair,
}: {
  title: string
  tips: [string, string, string]
  pair: Record<string, unknown>
}) {
  const before = isNum(pair['before']) ? pair['before'] : null
  const after = isNum(pair['after']) ? pair['after'] : null
  const change = after === null ? null : after - (before ?? 0)
  const cells = [
    ['Before Submission', before, tips[0]],
    ['After Submission', after, tips[1]],
    ['Change', change, tips[2]],
  ] as const
  return (
    <div className="flex flex-col gap-2">
      <span className="text-body-compact font-medium text-ink">{title}</span>
      <dl className="grid grid-cols-3 gap-4">
        {cells.map(([label, value, tip]) => (
          <div key={label} className="flex flex-col gap-1">
            <dt className="flex items-center gap-1 text-caption text-ink-subtle">
              {label}
              <Tip title={label}>{tip}</Tip>
            </dt>
            <dd
              className={cn(
                'num text-title',
                label === 'Change' && value !== null
                  ? TEXT_TONE[value > 0 ? 'profit' : value < 0 ? 'loss' : 'neutral']
                  : 'text-ink',
              )}
            >
              {value === null ? '-' : value.toLocaleString('en-US')}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  )
}

function Comparison({
  alpha,
  data,
}: {
  alpha: AlphaInfo
  data: Exclude<Awaited<ReturnType<typeof api.performance>>, { cached: false }>
}) {
  const delay = String(alpha.settings['delay'] ?? '')
  const scores = SCORES.flatMap((s) => {
    const pair = data[s.key]
    return pair && typeof pair === 'object'
      ? [
          <Score
            key={s.key}
            title={`Delay-${delay} ${s.title}`}
            tips={s.tips}
            pair={pair as Record<string, unknown>}
          />,
        ]
      : []
  })
  const before = data.stats?.before ?? {}
  const after = data.stats?.after
  const afterYears = data.yearlyStats?.after
  if (!after || !afterYears)
    return (
      <>
        {scores}
        <p className="text-body text-ink-subtle">No data available.</p>
      </>
    )
  const partitions = (data.partition ?? [])
    .map((path) =>
      String(
        path
          .split('.')
          .reduce<unknown>(
            (found, key) => (found as Record<string, unknown> | undefined)?.[key],
            alpha,
          ) ?? '',
      ),
    )
    .map((value) => PARTITION[value] ?? value)
    .join(', ')
  const properties = afterYears.schema?.properties ?? []
  const columns = COMPARED.flatMap((id) => {
    const at = properties.findIndex((p) => p.name === id)
    const p = properties[at]
    return p ? [{ ...p, at }] : []
  })
  const beforeRows = data.yearlyStats?.before?.records ?? []
  return (
    <div className="@container flex flex-col gap-4">
      {scores}
      <dl className={FIGURE_GRID}>
        {METRICS.map((m) => {
          const now = after[m.id] ?? 0
          return (
            <div key={m.id} className="flex flex-col gap-1">
              <dt className="flex items-center gap-1 text-caption text-ink-subtle">
                {m.title}
                <Tip title={m.title}>{m.tip}</Tip>
              </dt>
              <dd className="num flex flex-col text-title whitespace-nowrap text-ink">
                {`${bare(now, m.type)}${unit(m.type)}`}
                <span className="text-body-compact">
                  <Change value={now - (before[m.id] ?? 0)} type={m.type} />
                </span>
              </dd>
            </div>
          )
        })}
      </dl>
      <div className="flex flex-col gap-1">
        <h3 className="text-body font-medium text-ink">
          IS Performance After Submission ({partitions} alphas)
        </h3>
        <p className="text-body-compact text-ink-subtle">
          Combined performance data is shown for all {partitions} alphas. Submitting this alpha will
          not effect the combined performance of your other alphas.
        </p>
      </div>
      <div className="overflow-x-auto rounded-md border border-hairline">
        <table className="w-full text-body-compact whitespace-nowrap">
          <thead>
            <tr className="border-b border-hairline bg-surface-2 text-ink-subtle">
              {columns.map((c) => (
                <th
                  key={c.name}
                  className={cn(
                    'px-2 py-1.5 font-medium',
                    c.name === 'year' ? 'text-left' : 'text-right',
                  )}
                >
                  {c.title ?? c.name}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {(afterYears.records ?? []).map((record, i) => (
              <tr key={i} className="border-b border-hairline-subtle last:border-b-0">
                {columns.map((c) => {
                  const value = record[c.at]
                  const earlier = beforeRows[i]?.[c.at]
                  return (
                    <td
                      key={c.name}
                      className={cn('num px-2 py-1.5', c.name !== 'year' && 'text-right')}
                    >
                      {isNum(value) ? (
                        <span className="flex flex-col items-end">
                          <span className="text-ink">{`${bare(value, c.type)}${unit(c.type)}`}</span>
                          <Change value={value - (isNum(earlier) ? earlier : 0)} type={c.type} />
                        </span>
                      ) : (
                        String(value ?? '')
                      )}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {data.pnl?.records?.length ? (
        <div className="flex flex-col gap-2">
          <h3 className="text-body font-medium text-ink">PnL</h3>
          <PairChart set={data.pnl} label={`PnL before and after submitting ${alpha.alphaId}`} />
        </div>
      ) : null}
    </div>
  )
}

export function PerformanceComparison({ alpha }: { alpha: AlphaInfo }) {
  const { kept, run } = useBudgeted(['alpha', alpha.alphaId, 'performance'], (mode) =>
    api.performance(alpha.alphaId, mode),
  )
  const [open, setOpen] = useState(false)
  const data = kept.data?.cached ? kept.data : null
  const error = run.error ?? kept.error
  return (
    <Panel title="Performance Comparison" bodyClassName="flex flex-col gap-3">
      <RunBar
        what="Performance Comparison"
        lastRun={data?.fetchedAt}
        running={run.isPending}
        disabled={kept.isPending}
        open={open}
        onRun={() => run.mutate()}
        onToggle={() => {
          if (!data && !run.isPending) run.mutate()
          setOpen(!open)
        }}
      />
      {error && <ErrorNotice error={error} title="The comparison did not run" />}
      {open &&
        (run.isPending && !data ? (
          <Skeleton className="h-40" label="BRAIN is comparing" />
        ) : (
          data && <Comparison alpha={alpha} data={data} />
        ))}
    </Panel>
  )
}
