/**
 * The Alphas a screen is looking at, narrowed by the Power Pool Workflow in three steps, each
 * on what the one before kept: unsubmitted, then no check FAIL or ERROR, then Power Pool
 * Correlation passed. The survivors carry After-Cost Sharpe to sort them by.
 *
 * Shared by a task's own results and the Submittable list across every task, because the two
 * differ only in which Alphas they start from — a second copy would be a second place for the
 * Power Pool rules to drift.
 */

import { useMutation, useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { PlayIcon, RefreshCwIcon, SquareIcon } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { toast } from 'sonner'
import { tasks } from '@/api/core'
import { errorMessage } from '@/api/http'
import { cn } from '@/lib/cn'
import { DASH, fmt } from '@/lib/format'
import { useLive } from '@/lib/live'
import {
  AlphaTable,
  type AlphaTableRow,
  fromRanked,
  type Option,
  option,
  sortAlphas,
} from '@/screens/alpha-table'
import { Correlation } from '@/screens/alpha-table/columns'
import { BRAIN_ALPHA_URL } from '@/screens/pool/api'
import { alphaIds } from '@/screens/pool/detail'
import { labTasks, type RankedAlpha } from '@/screens/tasks/api'
import { type PowerPoolRow, powerPoolTone, usePowerPool } from '@/screens/tasks/power-pool'
import { MOD } from '@/shell/desktop'
import { Button, ErrorNotice, Kbd, LINK, Notice, Panel, Progress, Segmented } from '@/ui/kit'
import type { Sort } from '@/ui/table'

const WORKFLOW = 'power-pool-workflow'
const PROD_JOB = 'prod-correlation'

type Stored = RankedAlpha & { alphaId: string }

/** Step 1: not on the platform yet. */
const isUnsubmitted = (r: RankedAlpha): r is Stored => Boolean(r.alphaId) && !r.submitted
/** Step 2: no check FAIL or ERROR. PENDING is not a refusal. */
const isClean = (r: RankedAlpha) => r.submittable && r.refusedBy.length === 0
/** Step 3: below the ceiling, or past it with the Sharpe it takes. */
const passesPool = (row: PowerPoolRow | undefined) => {
  const tone = powerPoolTone(row)
  return tone === 'clear' || tone === 'beats'
}

/** The pane's own Power Pool Correlation: measured here, and toned by whether the Alpha's
 *  Sharpe beats the ones it exceeds. */
function poolOption(by: ReadonlyMap<string, PowerPoolRow>): Option {
  const found = (r: AlphaTableRow) => (r.alphaId ? by.get(r.alphaId) : undefined)
  return option('Correlation', 'powerPoolCorrelation', 'Power Pool Correlation', {
    width: 'minmax(120px,1.1fr)',
    align: 'right',
    value: (r) => found(r)?.correlation ?? null,
    cell: (r) => {
      const row = found(r)
      if (!row || row.correlation == null) return DASH
      const tone = powerPoolTone(row)
      return (
        <span
          className={cn(
            'num',
            tone === 'clear'
              ? 'text-pnl-positive-text'
              : tone === 'beats'
                ? 'text-status-warning'
                : tone === 'unmeasured'
                  ? 'text-ink-subtle'
                  : 'text-pnl-negative-text',
          )}
          title={
            tone === 'unmeasured'
              ? 'Not against every Power Pool Alpha of its region: some have no PnL stored'
              : undefined
          }
        >
          <Correlation value={row.correlation} />
        </span>
      )
    },
  })
}

/** The latest job of `kind`, running or just finished, from the shared background task list. */
function useJob(kind: string) {
  const live = useLive((s) => s.tasks)
  const polled = useQuery({
    queryKey: ['background-tasks'],
    queryFn: tasks.list,
    enabled: live == null,
    refetchInterval: 3000,
  })
  const jobs = ((live ?? polled.data)?.tasks ?? []).filter((t) => t.kind === kind)
  return jobs.find((t) => t.state === 'running') ?? jobs.at(-1)
}

/**
 * A click opens the row's Alpha in the detail sheet. Ctrl-click (Cmd on a Mac) opens it on
 * BRAIN instead, which is where the id is actually useful — so the table spends no column
 * printing one. Leaving for BRAIN takes the modifier a browser already uses for "open this
 * somewhere else".
 */
const openRow =
  (onOpen: ((alphaId: string, ids?: string[]) => void) | undefined) =>
  (r: AlphaTableRow, event: React.MouseEvent | React.KeyboardEvent, shown: AlphaTableRow[]) => {
    if (!r.alphaId) return
    if (event.ctrlKey || event.metaKey) {
      event.preventDefault()
      window.open(BRAIN_ALPHA_URL(r.alphaId), '_blank', 'noopener,noreferrer')
    } else onOpen?.(r.alphaId, alphaIds(shown))
  }

type Stage = 'all' | 'unsubmitted' | 'clean' | 'pool'

const Step = ({ name, count }: { name: string; count: number | null }) => (
  <>
    {name} <span className="num text-ink-subtle">{count == null ? DASH : fmt.int(count)}</span>
  </>
)

export function AlphaPane({
  title,
  view,
  rows,
  offer,
  defaults,
  sort,
  onSort,
  loading,
  error,
  onRefresh,
  onOpenAlpha,
  openAlpha,
  initial = 'pool',
}: {
  title: string
  /** Where the table's choice of columns is kept. */
  view: string
  rows: RankedAlpha[]
  offer: string[]
  defaults: string[]
  sort: Sort
  onSort: (sort: Sort) => void
  loading?: boolean
  error?: unknown
  /** Re-read the rows themselves: submitting an Alpha changes a flag that lives on the row. */
  onRefresh?: () => Promise<unknown>
  /** Show a row's Alpha in the detail sheet, as the task detail's own table does. */
  onOpenAlpha?: (alphaId: string, ids?: string[]) => void
  /** The Alpha open in the detail sheet, marked in the table. */
  openAlpha?: string | null
  /** The step of the funnel shown first. */
  initial?: Stage
}) {
  const [stage, setStage] = useState<Stage>(initial)
  const [refreshing, setRefreshing] = useState(false)
  const unsubmitted = useMemo(() => rows.filter(isUnsubmitted), [rows])
  const clean = useMemo(() => unsubmitted.filter(isClean), [unsubmitted])
  const cleanIds = useMemo(() => clean.map((r) => r.alphaId), [clean])
  const pool = usePowerPool(cleanIds)
  const survivors = useMemo(
    () => clean.filter((r) => passesPool(pool.by.get(r.alphaId))),
    [clean, pool.by],
  )
  const shown = useMemo(
    () => ({ all: rows, unsubmitted, clean, pool: survivors })[stage].map(fromRanked),
    [stage, rows, unsubmitted, clean, survivors],
  )
  const overrides = useMemo(() => [poolOption(pool.by)], [pool.by])

  const refresh = () => Promise.all([onRefresh?.(), cleanIds.length ? pool.refresh() : null])

  const job = useJob(WORKFLOW)
  const running = job?.state === 'running'
  // Read again once the job lands, so the table shows what it downloaded. Once per job: a
  // finished one lingers in the task list for a minute.
  const settled = useRef<string | null>(null)
  // The job seen running here, so opening the page on one that finished earlier does not
  // take over the reader's sort.
  const watched = useRef<string | null>(null)
  useEffect(() => {
    if (running) watched.current = job.id
    if (!job || running || settled.current === job.id) return
    settled.current = job.id
    void refresh()
    if (job.state === 'done' && watched.current === job.id) {
      onSort({ key: 'afterCostSharpe', desc: true })
    }
  })
  const prodJob = useJob(PROD_JOB)
  const prodRunning = prodJob?.state === 'running'
  // Fill the column in as BRAIN answers, at most twice a minute: reading the rows takes
  // seconds on a large account. Once more when the job lands.
  const prodRead = useRef({ at: Date.now(), watching: false })
  useEffect(() => {
    const read = prodRead.current
    if (prodRunning) {
      read.watching = true
      if (Date.now() - read.at < 30_000) return
      read.at = Date.now()
    } else if (read.watching) read.watching = false
    else return
    void onRefresh?.()
  })
  // The rows on show, top first: BRAIN's hourly limit means the ones read first come back first.
  const unchecked = sortAlphas(shown, sort, overrides).flatMap((r) =>
    r.alphaId && !r.submitted && r.prodCorrelation == null ? [r.alphaId] : [],
  )
  const checkProd = useMutation({
    mutationFn: () => labTasks.prodCorrelation(unchecked),
    onError: (e) =>
      toast.error('Could not start the Production Correlation check', {
        description: errorMessage(e),
      }),
  })
  const stopProd = useMutation({
    mutationFn: labTasks.stopProdCorrelation,
    onError: (e) => toast.error('Could not stop the check', { description: errorMessage(e) }),
  })
  const stopWorkflow = useMutation({
    mutationFn: labTasks.stopPowerPoolWorkflow,
    onError: (e) => toast.error('Could not stop the workflow', { description: errorMessage(e) }),
  })
  const start = useMutation({
    // Every Alpha here: the server runs the three steps itself rather than trusting ours.
    mutationFn: () => labTasks.powerPoolWorkflow(rows.flatMap((r) => r.alphaId ?? [])),
    onSuccess: () => setStage('pool'),
    onError: (e) => toast.error('Could not start the workflow', { description: errorMessage(e) }),
  })

  // No PnL of its own yet, or nothing in its region's pool to compare it with.
  const unmeasured =
    (pool.found?.withoutPnl.length ?? 0) +
    (pool.found?.rows.filter((r) => r.verdict === 'unmeasured').length ?? 0)
  const unsynced = pool.found?.scopes.flatMap((s) => s.poolWithoutPnl).length ?? 0
  const uncosted = survivors.filter((r) => r.afterCostSharpe == null).length
  // Opening on the last step with nothing measured yet would show an empty table over Alphas
  // that are clean: start one step back then, once, and leave the choice to the reader after.
  const opened = useRef(false)
  useEffect(() => {
    if (opened.current || !pool.found) return
    opened.current = true
    if (initial === 'pool' && survivors.length === 0 && clean.length > 0 && unmeasured > 0)
      setStage('clean')
  }, [pool.found, initial, survivors.length, clean.length, unmeasured])

  return (
    <Panel
      title={title}
      actions={
        <div className="flex flex-wrap items-center gap-3">
          <Button
            size="icon-sm"
            variant="ghost"
            aria-label="Read these Alphas again"
            title="Read again — after submitting an Alpha and syncing the Portfolio"
            onClick={() => {
              setRefreshing(true)
              void refresh().finally(() => setRefreshing(false))
            }}
          >
            <RefreshCwIcon className={cn((refreshing || pool.refreshing) && 'animate-spin')} />
          </Button>
          {prodRunning ? (
            <div className="flex w-52 flex-col gap-1">
              <span className="num text-caption text-ink-muted">
                Production Correlation · {prodJob.detail || prodJob.label}
              </span>
              <Progress value={prodJob.progress ?? null} label="Production Correlation progress" />
            </div>
          ) : null}
          {prodRunning ? (
            <Button
              size="sm"
              variant="secondary"
              loading={stopProd.isPending}
              onClick={() => stopProd.mutate()}
              title="Stop asking BRAIN. Every Production Correlation already back is kept."
            >
              <SquareIcon />
              Stop
            </Button>
          ) : (
            <Button
              size="sm"
              variant="secondary"
              disabled={unchecked.length === 0 || running}
              loading={checkProd.isPending}
              onClick={() => checkProd.mutate()}
              title={`Asks BRAIN for the ${fmt.int(unchecked.length)} shown without one, top of the table first. BRAIN allows only so many of these checks an hour, so a long list takes hours. No simulation quota.`}
            >
              Check Production Correlation
            </Button>
          )}
          {running ? (
            <>
              <div className="flex w-52 flex-col gap-1">
                <span className="num text-caption text-ink-muted">{job.detail || job.label}</span>
                <Progress value={job.progress ?? null} label="Power Pool Workflow progress" />
              </div>
              <Button
                size="sm"
                variant="secondary"
                loading={stopWorkflow.isPending}
                onClick={() => stopWorkflow.mutate()}
                title="Stop the workflow. Every PnL and turnover already downloaded is kept."
              >
                <SquareIcon />
                Stop
              </Button>
            </>
          ) : (
            <Button
              size="sm"
              variant="primary"
              disabled={cleanIds.length === 0 || prodRunning}
              loading={start.isPending}
              onClick={() => start.mutate()}
              title="Downloads PnL where Power Pool Correlation is unmeasured, then turnover for the Alphas that satisfy it. No simulation quota."
            >
              <PlayIcon />
              Run Power Pool Workflow
            </Button>
          )}
        </div>
      }
    >
      {/* The funnel, each step one click: every Alpha down to the ones worth submitting. */}
      <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-2">
        <Segmented
          label="Which Alphas"
          value={stage}
          onChange={setStage}
          items={[
            { value: 'all', label: <Step name="All" count={rows.length} /> },
            { value: 'unsubmitted', label: <Step name="Unsubmitted" count={unsubmitted.length} /> },
            { value: 'clean', label: <Step name="No Checks FAIL or ERROR" count={clean.length} /> },
            {
              value: 'pool',
              label: (
                <Step
                  name="Pass Power Pool Correlation"
                  count={pool.found ? survivors.length : null}
                />
              ),
            },
          ]}
        />
        {(unmeasured > 0 || uncosted > 0) && (
          <span className="num text-body-compact text-ink-subtle">
            {[
              unmeasured > 0 && `${fmt.int(unmeasured)} not measured yet`,
              uncosted > 0 && `${fmt.int(uncosted)} without After-Cost Sharpe yet`,
            ]
              .filter(Boolean)
              .join(' · ')}
          </span>
        )}
        <span className="ml-auto hidden items-center gap-1 text-caption text-ink-subtle xl:inline-flex">
          <Kbd>{MOD}Click</Kbd> a row opens it on BRAIN
        </span>
      </div>
      {job?.state === 'failed' && (
        <Notice tone="error" className="mb-3" title="The Power Pool Workflow stopped">
          {job.error}
        </Notice>
      )}
      {unsynced > 0 && (
        <Notice tone="warn" className="mb-3" title="Sync your Portfolio first">
          {fmt.int(unsynced)} submitted Power Pool Alpha{unsynced === 1 ? ' has' : 's have'} no PnL
          stored, so Power Pool Correlation is not measured against {unsynced === 1 ? 'it' : 'them'}
          . Sync from BRAIN on the{' '}
          <Link to="/portfolio" className={LINK}>
            Portfolio
          </Link>{' '}
          page, then read these again.
        </Notice>
      )}
      {pool.error != null && (
        <ErrorNotice error={pool.error} title="Could not measure Power Pool correlation" />
      )}
      <AlphaTable
        view={view}
        label={title}
        rows={shown}
        offer={offer}
        defaults={defaults}
        overrides={overrides}
        onRowClick={openRow(onOpenAlpha)}
        openAlpha={openAlpha}
        sort={sort}
        onSort={onSort}
        loading={(loading ?? false) || (stage === 'pool' && cleanIds.length > 0 && pool.pending)}
        error={error}
        maxHeight="70vh"
        empty={
          rows.length === 0
            ? 'No Alphas back yet.'
            : stage !== 'pool'
              ? 'No Alpha gets this far.'
              : unmeasured > 0
                ? 'Run Power Pool Workflow to measure the rest.'
                : 'No Alpha here satisfies Power Pool Correlation.'
        }
      />
    </Panel>
  )
}
