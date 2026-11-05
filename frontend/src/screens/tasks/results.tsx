/**
 * One task's whole result set, with the statistics the task card has no room for.
 *
 * Built for a Settings Sampler sweep, which runs the same expression across many markets: the
 * question there is not "which Alpha is best" but "where does this idea work", and that is a
 * comparison across regions rather than a leaderboard.
 */

import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from '@tanstack/react-router'
import { ArrowLeftIcon } from 'lucide-react'
import { useMemo, useState } from 'react'
import { cn } from '@/lib/cn'
import { CORE_ORDER, DASH, fmt } from '@/lib/format'
import { useRefetchOn } from '@/lib/ws'
import { useDetailSheet } from '@/screens/pool/detail'
import { AstInspector } from '@/screens/pool/shared'
import { AlphaPane } from '@/screens/tasks/alpha-pane'
import { labTasks, type RankedAlpha, TOP_LIMIT } from '@/screens/tasks/api'
import {
  Figure,
  METRICS,
  metricHeader,
  setting,
  TASK_OFFER,
  taskStatus,
} from '@/screens/tasks/columns'
import { Button, Empty, ErrorNotice, KV, Metric, Page, PageHeader, Panel, Skeleton } from '@/ui/kit'
import { type Column, DataTable, type Sort } from '@/ui/table'

const market = (r: RankedAlpha) => setting(r, 'region') || DASH

const everyAlphaDefaults = (agent: boolean) => [
  'alphaId',
  'region',
  'delay',
  'universe',
  'neutralization',
  'investability',
  ...(agent ? ['truncation'] : []),
  'powerPoolCorrelation',
  'prodCorrelation',
  'failedChecks',
  ...CORE_ORDER,
  'afterCostSharpe',
  'testSharpe',
  'testFitness',
]

/**
 * What a reader can narrow the table by. Three independent questions, each answerable on its
 * own, so any combination of them is a valid thing to ask: the useful one is all three at
 * once, but "which of my refused Alphas still survive costs" is a real question too and a
 * fixed set of presets could not express it.
 */
/** One row of the across-regions table. */
interface RegionRow {
  region: string
  alphas: number
  submittable: number
  failed: number
  /** Per metric, the best any *submittable* Alpha here reached, keyed by its column key. */
  best: Record<string, number | null>
}

/** One distinct set of failed checks, and how much of the sweep landed on exactly that set. */
interface CheckSet {
  key: string
  checks: string[]
  alphas: number
  share: number
}

/**
 * What is actually stopping this idea, counted by the *combination* of checks rather than by
 * check. One Alpha usually trips several at once, so a per-check tally double-counts and
 * makes every sweep look like the same wall of Sharpe failures. The sets separate "only the
 * turnover is wrong", which a decay or a gate can fix, from "the signal is weak everywhere".
 */
function CheckSets({ rows }: { rows: RankedAlpha[] }) {
  const [sort, setSort] = useState<Sort>({ key: 'alphas', desc: true })

  const sets = useMemo(() => {
    const seen = new Map<string, CheckSet>()
    for (const r of rows) {
      const checks = [...r.refusedBy].sort()
      const key = checks.join(' + ')
      const found = seen.get(key) ?? { key, checks, alphas: 0, share: 0 }
      found.alphas += 1
      seen.set(key, found)
    }
    const total = rows.length || 1
    return [...seen.values()].map((s) => ({ ...s, share: s.alphas / total }))
  }, [rows])

  const sorted = useMemo(() => {
    const pick = (s: CheckSet): string | number =>
      sort.key === 'key' ? s.key : sort.key === 'count' ? s.checks.length : s.alphas
    return [...sets].sort((a, b) => {
      const x = pick(a)
      const y = pick(b)
      const order = typeof x === 'string' ? x.localeCompare(String(y)) : Number(x) - Number(y)
      return sort.desc ? -order : order
    })
  }, [sets, sort])

  return (
    <Panel
      title="Checks Failed Together"
      description="Every distinct set of refusing checks, and how many Alphas hit exactly that set. Sort by Checks to find the ones a single problem is holding back."
      actions={<span className="num text-ink-subtle">{fmt.int(sets.length)} sets</span>}
    >
      <DataTable
        label="Failed check sets"
        rows={sorted}
        rowKey={(s) => s.key || 'none'}
        sort={sort}
        onSort={setSort}
        columns={[
          {
            key: 'key',
            header: 'Checks Failed',
            width: 'minmax(260px,4fr)',
            sortable: true,
            cell: (s) =>
              s.checks.length === 0 ? (
                <span className="text-ink-subtle">{DASH}</span>
              ) : (
                <span className="num truncate text-pnl-negative-text" title={s.checks.join(', ')}>
                  {s.checks.join(', ')}
                </span>
              ),
          },
          {
            key: 'count',
            header: 'Checks',
            width: 'minmax(72px,0.6fr)',
            align: 'right',
            sortable: true,
            cell: (s) => <span className="num">{fmt.int(s.checks.length)}</span>,
          },
          {
            key: 'alphas',
            header: 'Alphas',
            width: 'minmax(76px,0.6fr)',
            align: 'right',
            sortable: true,
            cell: (s) => (
              <span className={cn('num', s.checks.length === 0 && 'text-pnl-positive-text')}>
                {fmt.int(s.alphas)}
              </span>
            ),
          },
          {
            key: 'share',
            header: 'Share',
            width: 'minmax(76px,0.6fr)',
            align: 'right',
            cell: (s) => <span className="num text-ink-subtle">{fmt.pct(s.share, 1)}</span>,
          },
        ]}
        maxHeight="40vh"
        empty="No Alphas back yet."
      />
    </Panel>
  )
}

export function TaskResultsScreen() {
  const { taskId } = useParams({ from: '/tasks/$taskId' })
  const id = Number(taskId)
  const [sort, setSort] = useState<Sort>({ key: 'sharpe', desc: true })
  const detail = useDetailSheet()

  const tasks = useQuery({ queryKey: ['lab-tasks'], queryFn: labTasks.list })
  const task = tasks.data?.tasks.find((t) => t.id === id)

  const top = useQuery({
    queryKey: ['lab-tasks', id, 'results'],
    // The sweep's whole result set, not a page of it: the comparison needs every row.
    queryFn: () => labTasks.top(id, TOP_LIMIT),
    enabled: Number.isFinite(id),
  })
  const rows = useMemo(() => top.data ?? [], [top.data])

  const byRegion = useMemo(() => {
    const seen = new Map<string, RegionRow>()
    for (const r of rows) {
      const key = market(r)
      const row = seen.get(key) ?? {
        region: key,
        alphas: 0,
        submittable: 0,
        failed: 0,
        best: {},
      }
      row.alphas += 1
      const green = r.submittable || r.pending
      if (green) row.submittable += 1
      else row.failed += 1
      // Only Alphas nothing refused: a best that BRAIN will not take is not a best, it is a
      // reason to look in this region and be disappointed.
      if (green) {
        for (const m of METRICS) {
          const value = r[m.key] as number | null
          if (value == null) continue
          const held = row.best[m.key]
          const better = held == null || (m.best === 'max' ? value > held : value < held)
          if (better) row.best[m.key] = value
        }
      }
      seen.set(key, row)
    }
    return [...seen.values()].sort((a, b) => b.submittable - a.submittable || b.alphas - a.alphas)
  }, [rows])

  // Shown only when there is one expression to show. A Settings Sampler sweep is the same
  // expression everywhere, which is the whole point of it; a Search Lab task is a thousand
  // different ones and has no single answer to put here.
  // Read off the task, not the results: a Settings Sampler sweep was *given* an expression
  // and the four settings it holds still, so those are facts about the task. Recovering them
  // by scanning a thousand rows for whatever happens to agree is a guess that looks like one.
  const held: [string, string][] = task
    ? (
        [
          ['Decay', task.decay],
          ['Truncation', task.truncationAgent ? 'Truncation Agent' : task.truncation],
          ['NaN Handling', task.nanHandling],
          ['Test Period', task.testPeriod],
          // Which models answered, when a Power Pool task moved between them.
          [
            'Models',
            Object.entries(task.llmModels ?? {})
              .map(([model, calls]) => `${model} × ${fmt.int(calls)}`)
              .join(', '),
          ],
        ].filter(([, v]) => v != null && v !== '') as [string, string | number][]
      ).map(([label, value]) => [label, String(value)])
    : []

  // The download reports progress through the task registry, so the rows refresh as its
  // broadcasts land rather than on a timer of their own.
  useRefetchOn('tasks', ['lab-tasks', id, 'results'], 5000)
  // The lab itself reports on its own topic: the task's status, and the rows with it.
  useRefetchOn('studies', ['lab-tasks'], 10_000)

  const green = rows.filter((r) => r.submittable || r.pending).length
  const pending = rows.filter((r) => r.pending).length

  return (
    <Page>
      <PageHeader
        title={task?.name || task?.templateName || task?.labName || `Task ${taskId}`}
        // The lab is the title already; repeating it here spent the one line that says
        // where this task got to.
        description={
          task
            ? `${taskStatus(task).label}${task.alphaId ? ` · ${task.alphaId}` : ''}`
            : 'Every Alpha this task produced'
        }
        actions={
          <Button variant="ghost" render={<Link to="/tasks" search={{ task: id }} />}>
            <ArrowLeftIcon />
            Back to Tasks
          </Button>
        }
      />

      {top.isError && <ErrorNotice error={top.error} title="Could not read this task's results" />}

      {task?.expression && (
        <Panel
          title="Alpha Expression"
          description="What every simulation in this task ran, and the settings it held still."
        >
          <div className="flex flex-col gap-4">
            <AstInspector expression={task.expression} />
            {held.length > 0 && (
              <KV
                items={held}
                className="border-t border-hairline pt-4 sm:grid-cols-[repeat(2,auto_minmax(0,1fr))] lg:grid-cols-[repeat(4,auto_minmax(0,1fr))]"
              />
            )}
          </div>
        </Panel>
      )}

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Metric
          boxed
          label="Alphas"
          value={fmt.int(rows.length)}
          hint={
            rows.length >= TOP_LIMIT && task
              ? `The best ${fmt.int(TOP_LIMIT)} of ${fmt.int(task.simulated)} simulated`
              : ''
          }
        />
        <Metric
          boxed
          tone="profit"
          label="Submittable"
          value={`${pending > 0 ? '~' : ''}${fmt.int(green)}`}
        />
        <Metric
          boxed
          tone={rows.length - green > 0 ? 'loss' : 'neutral'}
          label="Refused"
          value={fmt.int(rows.length - green)}
        />
        <Metric boxed label="Regions" value={fmt.int(byRegion.length)} />
      </div>

      <Panel
        title="Across Regions"
        description="Where the same expression is worth submitting, and the best each metric reaches there. Refused Alphas are counted, never counted as a best."
      >
        {top.isPending ? (
          <Skeleton className="h-40" />
        ) : byRegion.length === 0 ? (
          <Empty title="No Alphas back yet." />
        ) : (
          <DataTable
            label="Results by region"
            rows={byRegion}
            rowKey={(r) => r.region}
            columns={[
              {
                key: 'region',
                header: 'Region',
                width: 'minmax(80px,1fr)',
                cell: (r) => <span className="num text-ink">{r.region}</span>,
              },
              {
                key: 'alphas',
                header: 'Alphas',
                width: 'minmax(80px,0.8fr)',
                align: 'right',
                cell: (r) => fmt.int(r.alphas),
              },
              {
                key: 'submittable',
                header: 'Submittable',
                width: 'minmax(96px,1fr)',
                align: 'right',
                cell: (r) => (
                  <span className={cn('num', r.submittable > 0 && 'text-pnl-positive-text')}>
                    {fmt.int(r.submittable)}
                  </span>
                ),
              },
              {
                key: 'failed',
                header: 'Refused',
                width: 'minmax(84px,0.8fr)',
                align: 'right',
                cell: (r) => (
                  <span className={cn('num', r.failed > 0 && 'text-pnl-negative-text')}>
                    {fmt.int(r.failed)}
                  </span>
                ),
              },
              ...METRICS.map(
                (m): Column<RegionRow> => ({
                  key: String(m.key),
                  header: metricHeader(m),
                  width: 'minmax(78px,0.8fr)',
                  align: 'right',
                  cell: (r) => <Figure metric={m} value={r.best[m.key] ?? null} />,
                }),
              ),
            ]}
            maxHeight="40vh"
            empty="No Alphas back yet."
          />
        )}
      </Panel>

      <AlphaPane
        title="Every Alpha"
        initial="all"
        view="every-alpha"
        rows={rows}
        offer={TASK_OFFER}
        defaults={everyAlphaDefaults(Boolean(task?.truncationAgent))}
        sort={sort}
        onSort={setSort}
        loading={top.isPending}
        error={top.error}
        onRefresh={() => top.refetch()}
        onOpenAlpha={detail.open}
        openAlpha={detail.openId}
      />

      <CheckSets rows={rows} />
      {detail.sheet}
    </Page>
  )
}
