/**
 * Portfolio: submitted Alphas combined at equal weight, the way BRAIN combines its own pool.
 * Genius scores Combined Alpha Performance and Combined Power Pool Alpha Performance; filter
 * by the Power Pool Alpha classification for the second.
 */

import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CopyIcon, RefreshCwIcon } from 'lucide-react'
import { Fragment, useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { tasks } from '@/api/core'
import { errorMessage } from '@/api/http'
import { REGION_AGNOSTIC_CLASS } from '@/api/platform'
import { cn } from '@/lib/cn'
import { CORE_METRICS, CORE_ORDER, DASH, fmt } from '@/lib/format'
import { useLive } from '@/lib/live'
import { useDebounced } from '@/lib/use-debounced'
import {
  AlphaTable,
  type AlphaTableRow,
  CostInput,
  fromMember,
  useCostBps,
  useNetBps,
} from '@/screens/alpha-table'
import { INVESTABILITY } from '@/screens/alpha-table/columns'
import { alphaIds, useDetailSheet } from '@/screens/pool/detail'
import {
  Button,
  Chips,
  Disclosure,
  ErrorNotice,
  figureTone,
  Notice,
  Page,
  PageHeader,
  Panel,
  Progress,
  Skeleton,
  signTone,
  TEXT_TONE,
  Textarea,
} from '@/ui/kit'
import { type Column, DataTable } from '@/ui/table'
import {
  type PortfolioMember,
  type PortfolioResult,
  type PortfolioStats,
  portfolio,
  type Windows,
} from './api'
import { ChartLegend, PortfolioChart } from './chart'
import { CorrelationMatrix } from './correlation'
import { PyramidPicker } from './pyramid-grid'
import { compile, SelectionError } from './selection'
import { SelectionInfo } from './selection-info'

/** One block of filter chips. Within a block any match keeps an Alpha; blocks combine with AND. */
interface Facet {
  key: string
  title: string
  values: (m: PortfolioMember) => string[]
}

const FACETS: Facet[] = [
  { key: 'classification', title: 'Classifications', values: (m) => m.classifications },
  {
    key: 'investability',
    title: 'Investability Constraints',
    values: (m) => [INVESTABILITY[m.investability]],
  },
  { key: 'region', title: 'Region', values: (m) => (m.region ? [m.region] : []) },
  { key: 'delay', title: 'Delay', values: (m) => (m.delay === null ? [] : [`D${m.delay}`]) },
  // Category is a row of the Pyramid grid, so it needs no filter of its own.
  { key: 'pyramid', title: 'Pyramids', values: (m) => m.pyramids },
]

const facetOf = (key: string) => FACETS.find((f) => f.key === key) as Facet

/** Where the table's choice of columns and its cost are kept. */
const VIEW = 'portfolio'

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

/** The latest Portfolio sync, running or just finished. */
function useLastSync() {
  const live = useLive((s) => s.tasks)
  const polled = useQuery({
    queryKey: ['background-tasks'],
    queryFn: tasks.list,
    enabled: live == null,
    refetchInterval: 3000,
  })
  return ((live ?? polled.data)?.tasks ?? []).filter((t) => t.kind === 'portfolio-sync').at(-1)
}

/** Refreshes the page as a Portfolio sync lands PnL, and when it ends: no other job changes it,
 *  and every recompute reads every submitted Alpha's series. */
function useRefreshOnSync() {
  const queryClient = useQueryClient()
  const sync = useLastSync()
  const at = sync ? `${sync.id} ${sync.state} ${sync.progress}` : ''
  const seen = useRef(at)
  useEffect(() => {
    if (seen.current === at) return
    seen.current = at
    void queryClient.invalidateQueries({ queryKey: ['portfolio'] })
  }, [at, queryClient])
}

function useSyncTask() {
  const last = useLastSync()
  return last?.state === 'running' ? last : undefined
}

/** IDs shown before the rest are counted: a large account has hundreds. */
const SHOWN_IDS = 12

function SyncProgress() {
  const task = useLastSync()
  if (!task) return null
  if (task.state === 'failed')
    return (
      <Notice tone="error" title="The sync stopped">
        {task.error}
      </Notice>
    )
  if (task.state !== 'running')
    return task.detail ? <p className="text-body-compact text-ink-muted">{task.detail}</p> : null
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-body-compact text-ink-muted">{task.detail ?? task.label}</span>
      <Progress value={task.progress ?? null} label="Sync progress" />
    </div>
  )
}

function SyncButton() {
  const queryClient = useQueryClient()
  const running = useSyncTask() !== undefined
  const sync = useMutation({
    mutationFn: portfolio.sync,
    onSuccess: () => {
      toast.success('Syncing your SUBMITTED Alphas from BRAIN')
      void queryClient.invalidateQueries({ queryKey: ['portfolio'] })
    },
  })
  const busy = sync.isPending || running
  return (
    <Button variant="primary" loading={busy} onClick={() => sync.mutate()}>
      {!busy && <RefreshCwIcon />}
      Sync from BRAIN
    </Button>
  )
}

export function PortfolioScreen() {
  const netNow = useNetBps(VIEW)
  const charged = useDebounced(netNow, 400)
  const members = useQuery({
    queryKey: ['portfolio', 'members', charged ?? null],
    queryFn: () => portfolio.members(charged),
    placeholderData: keepPreviousData,
  })
  useRefreshOnSync()

  const [picked, setPicked] = useState<Record<string, string[]>>({})
  const [selection, setSelection] = useState('')
  const quietSelection = useDebounced(selection, 300)
  /** Ticked or unticked by hand, over what the filters pick. A filter change clears it. */
  const [overrides, setOverrides] = useState<ReadonlyMap<string, boolean>>(new Map())
  const detail = useDetailSheet()
  const costNow = useCostBps(VIEW)
  const cost = useDebounced(costNow, 400)

  const all = members.data?.members ?? []
  const metricNames = Object.keys(all[0]?.metrics ?? {}).sort()
  let rule: ReturnType<typeof compile> | undefined
  let ruleError: string | undefined
  try {
    rule = compile(quietSelection, metricNames)
  } catch (e) {
    if (!(e instanceof SelectionError)) throw e
    ruleError = e.message
  }
  const matches = (m: PortfolioMember) =>
    FACETS.every((f) => {
      const want = picked[f.key] ?? []
      return !want.length || f.values(m).some((v) => want.includes(v))
    }) &&
    (rule?.test(m.metrics) ?? true)
  const choose = (key: string, value: string[]) => {
    setPicked((prev) => ({ ...prev, [key]: value }))
    setOverrides(new Map())
  }
  const block = (key: string, size?: 'lg') => (
    <FacetBlock
      facet={facetOf(key)}
      members={all}
      value={picked[key] ?? []}
      onChange={(v) => choose(key, v)}
      {...(size ? { size } : {})}
    />
  )
  const isIncluded = (m: PortfolioMember) => overrides.get(m.alphaId) ?? matches(m)
  // Ticked Alphas first, each group in its original order.
  const rows = [...all.filter(isIncluded), ...all.filter((m) => !isIncluded(m))]
  const included = all.filter(isIncluded).map((m) => m.alphaId)
  const ids = [...included].sort()
  const copy = useMutation({
    mutationFn: () => portfolio.markdown(included),
    onSuccess: async ({ text, unsynced }) => {
      await navigator.clipboard.writeText(text)
      toast.success(`Copied ${included.length} Alpha${included.length === 1 ? '' : 's'}`)
      if (unsynced > 0)
        toast.warning(
          `Language, Pasteurization and NaN Handling are not stored yet for ${unsynced} of them. Sync from BRAIN, then copy again.`,
        )
    },
    onError: (e: unknown) => toast.error(errorMessage(e)),
  })

  const computed = useQuery({
    queryKey: ['portfolio', 'compute', ids, cost],
    queryFn: () => portfolio.compute(ids, cost),
    enabled: ids.length > 0,
    placeholderData: keepPreviousData,
  })
  // A disabled query keeps its last answer, which would outlive deselecting every Alpha.
  const result = ids.length ? computed.data : undefined
  const unlabelled = all.filter((m) => !m.labelled).length
  // Shown while newer figures are on their way, so an old answer never reads as the new one.
  const membersStale = members.isPlaceholderData || netNow !== charged
  const combinedStale = computed.isPlaceholderData || costNow !== cost
  const syncing = useSyncTask() !== undefined

  return (
    <Page>
      <PageHeader title="Portfolio" actions={<SyncButton />} />
      <SyncProgress />
      {members.error ? <ErrorNotice error={members.error} /> : null}
      {computed.error ? <ErrorNotice error={computed.error} /> : null}
      {unlabelled > 0 && (
        <Notice tone="warn" title="Classifications and pyramids not read yet">
          Sync from BRAIN to read them for {unlabelled} SUBMITTED Alpha
          {unlabelled === 1 ? '' : 's'}.
        </Notice>
      )}
      {result && result.missing.length > 0 && (
        <Notice
          tone={syncing ? 'info' : 'warn'}
          title={
            syncing
              ? `Downloading PnL: ${fmt.int(result.missing.length)} Alphas to go`
              : `${fmt.int(result.missing.length)} Alpha${result.missing.length === 1 ? ' has' : 's have'} no PnL stored`
          }
        >
          {syncing
            ? 'They join the charts below as they arrive.'
            : 'They are left out until it is downloaded. Sync from BRAIN to download it; one that stays after a sync is one BRAIN gave no PnL for.'}{' '}
          <span className="num text-ink-subtle">
            {result.missing.slice(0, SHOWN_IDS).join(', ')}
            {result.missing.length > SHOWN_IDS &&
              ` and ${fmt.int(result.missing.length - SHOWN_IDS)} more`}
          </span>
        </Notice>
      )}

      <Panel
        title="Alphas"
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-body text-ink-muted">
              <span className="num text-ink">{included.length}</span> of{' '}
              <span className="num">{all.length}</span> SUBMITTED selected
            </span>
            {/* Back to every Alpha: filters, Selection Expression and hand ticks all cleared, so
                no chip stays pressed over a selection it no longer makes. Ticking all or none
                by hand is the table's own header box. */}
            {(Object.values(picked).some((v) => v.length > 0) ||
              selection.trim() !== '' ||
              overrides.size > 0) && (
              <Button
                size="sm"
                variant="ghost"
                onClick={() => {
                  setPicked({})
                  setSelection('')
                  setOverrides(new Map())
                }}
              >
                Reset Filters
              </Button>
            )}
            <Button
              size="sm"
              variant="ghost"
              loading={copy.isPending}
              disabled={!included.length}
              onClick={() => copy.mutate()}
            >
              {!copy.isPending && <CopyIcon />}
              Copy Alphas
            </Button>
          </div>
        }
        bodyClassName="flex flex-col gap-4"
      >
        <div className="flex flex-col gap-5">
          {block('classification', 'lg')}
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4 *:rounded-md *:border *:border-hairline *:bg-surface-2 *:p-3">
            {block('region')}
            {block('delay')}
            {block('investability')}
            <PyramidPicker
              members={all}
              value={picked['pyramid'] ?? []}
              onChange={(v) => choose('pyramid', v)}
            />
          </div>
          <Disclosure
            summary={
              rule?.conditions
                ? `Selection Expression (${rule.conditions} condition${rule.conditions === 1 ? '' : 's'})`
                : 'Selection Expression'
            }
          >
            <div className="flex items-start gap-2">
              <div className="flex min-w-0 flex-1 flex-col gap-1.5">
                <Textarea
                  className="font-mono"
                  rows={4}
                  spellCheck={false}
                  aria-label="Selection Expression"
                  aria-invalid={ruleError !== undefined}
                  placeholder={
                    'information_ratio >= 0.10;\nreturns / turnover >= 1;\nreturns / drawdown >= 1'
                  }
                  value={selection}
                  onChange={(e) => {
                    setSelection(e.target.value)
                    setOverrides(new Map())
                  }}
                />
                {ruleError && (
                  <span
                    role="alert"
                    className="text-body-compact break-words text-pnl-negative-text"
                  >
                    {ruleError}. Not applied until it reads.
                  </span>
                )}
              </div>
              <SelectionInfo metrics={metricNames} />
            </div>
          </Disclosure>
        </div>
        <div className={cn('transition-opacity', membersStale && 'opacity-50')}>
          <MembersTable
            rows={rows}
            selected={new Set(included)}
            onSelect={(ids, on) =>
              setOverrides((prev) => new Map([...prev, ...ids.map((id) => [id, on] as const)]))
            }
            onOpen={detail.open}
            openId={detail.openId}
            loading={members.isPending}
          />
        </div>
      </Panel>
      {detail.sheet}

      {ids.length > 0 && !result ? (
        computed.error ? null : (
          <Skeleton className="h-96" label="Combining Alphas" />
        )
      ) : !result || result.alphas === 0 ? (
        <Panel>
          <p className="text-body text-ink-subtle">
            {all.length === 0
              ? 'No SUBMITTED Alphas stored yet. Sync from BRAIN to fetch them.'
              : ids.length > 0
                ? `None of the ${fmt.int(ids.length)} selected Alphas has PnL stored yet. Sync from BRAIN to download it.`
                : 'Select at least one Alpha.'}
          </p>
        </Panel>
      ) : (
        <div
          className={cn('flex flex-col gap-4 transition-opacity', combinedStale && 'opacity-50')}
        >
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
                <CostInput view={VIEW} />
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
                  Each square is a pair of Alphas, in submission order: newest at the top left,
                  oldest at the bottom right. Hover a square for the pair.
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
              <CorrelationMatrix result={result} onOpen={detail.open} />
            </Panel>
          )}
        </div>
      )}
    </Page>
  )
}

function FacetBlock({
  facet,
  members,
  value,
  onChange,
  size,
}: {
  facet: Facet
  members: PortfolioMember[]
  value: string[]
  onChange: (value: string[]) => void
  size?: 'lg'
}) {
  const counts = new Map<string, number>()
  for (const m of members) for (const v of facet.values(m)) counts.set(v, (counts.get(v) ?? 0) + 1)
  // Region Agnostic counts what was submitted: one parent, however many of its children
  // passed. Choosing it still selects every child, the Alphas that carry the class.
  const children = counts.get(REGION_AGNOSTIC_CLASS)
  const parents = new Set(members.flatMap((m) => (m.raParent ? [m.raParent] : []))).size
  if (children !== undefined) counts.set(REGION_AGNOSTIC_CLASS, parents)
  const items = [...counts.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([v, n]) => ({
      value: v,
      ...(v === REGION_AGNOSTIC_CLASS && children !== undefined
        ? {
            title: `${parents} Region Agnostic Alpha${parents === 1 ? '' : 's'} · ${children} child Alpha${children === 1 ? '' : 's'}`,
          }
        : {}),
      label:
        size === 'lg' ? (
          <span className="flex w-full items-center justify-between gap-3">
            {v}
            <span className="num text-ink-muted">{n}</span>
          </span>
        ) : (
          `${v} · ${n}`
        ),
    }))
  return (
    <div className="flex min-w-0 flex-col gap-2">
      <h3 className="text-body-compact font-medium text-ink-muted">{facet.title}</h3>
      {items.length ? (
        <Chips
          label={facet.title}
          items={items}
          value={value}
          onChange={onChange}
          {...(size ? { size } : {})}
        />
      ) : (
        <span className="text-body-compact text-ink-subtle">{DASH}</span>
      )}
    </div>
  )
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

interface FamilyShape {
  parent: string
  /** Where the row sits in a run of its family's rows, as the table shows them. */
  first: boolean
  last: boolean
}

/**
 * A region-agnostic family's rows, joined: BRAIN submits a parent's passing children together,
 * so they share a parent and a submission time and sit next to each other. A sort that
 * splits them gives each piece its own block.
 */
function familyShapes(rows: AlphaTableRow[]): Map<string, FamilyShape> {
  const shapes = new Map<string, FamilyShape>()
  rows.forEach((m, i) => {
    if (!m.raParent) return
    shapes.set(m.key, {
      parent: m.raParent,
      first: rows[i - 1]?.raParent !== m.raParent,
      last: rows[i + 1]?.raParent !== m.raParent,
    })
  })
  return shapes
}

/**
 * A family's rows drawn as one block: a tinted box opening on its first row and closing on
 * its last. The edges are an overlay rather than the row's own border, which would narrow the
 * row and shift its columns out of line with every other row's.
 */
function familyRow(shape: FamilyShape | undefined): string | undefined {
  if (!shape) return undefined
  return cn(
    'bg-primary-subtle hover:bg-primary-subtle',
    'after:pointer-events-none after:absolute after:inset-0 after:border-x after:border-primary/45',
    shape.first && 'rounded-t-md after:rounded-t-md after:border-t',
    shape.last
      ? 'rounded-b-md border-b-transparent after:rounded-b-md after:border-b'
      : 'border-b-primary/15',
  )
}

/** What the Portfolio's Alphas have: every column but a task's own. */
const OFFER = [
  'alphaId',
  'name',
  'dateSubmitted',
  'classifications',
  'tags',
  'pyramids',
  'categories',
  'expression',
  'operatorCount',
  'region',
  'delay',
  'universe',
  'investability',
  'neutralization',
  'decay',
  'truncation',
  ...CORE_ORDER,
  'trainSharpe',
  'testSharpe',
  'kRatio',
  'incrementalSharpe',
  'longCount',
  'shortCount',
  'imbalance',
  'prodCorrelation',
  'selfCorrelation',
  'powerPoolCorrelation',
  'portfolioCorrelation',
  'maxPairwiseCorrelation',
]

const DEFAULTS = [
  'alphaId',
  'classifications',
  'region',
  'delay',
  'universe',
  'investability',
  ...CORE_ORDER,
  'prodCorrelation',
]

function MembersTable({
  rows,
  selected,
  onSelect,
  onOpen,
  openId,
  loading,
}: {
  rows: PortfolioMember[]
  selected: ReadonlySet<string>
  onSelect: (ids: string[], on: boolean) => void
  /** Show the row's Alpha in the side pane. */
  onOpen: (alphaId: string, ids?: string[]) => void
  openId: string | null
  loading: boolean
}) {
  const shaped = rows.map(fromMember)
  return (
    <AlphaTable
      view={VIEW}
      label="SUBMITTED Alphas"
      rows={shaped}
      offer={OFFER}
      defaults={DEFAULTS}
      afterCost="screen"
      selected={selected}
      onSelect={onSelect}
      onRowClick={(m, _event, shown) => m.alphaId && onOpen(m.alphaId, alphaIds(shown))}
      openAlpha={openId}
      rowClass={(shown) => {
        const families = familyShapes(shown)
        return (m) => familyRow(families.get(m.key))
      }}
      loading={loading}
      maxHeight="28rem"
      empty="No SUBMITTED Alpha matches these filters."
    />
  )
}
