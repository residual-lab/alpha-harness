/**
 * Every submittable Alpha from every task, on the same pane a single task's results use.
 */

import { useMutation, useQuery } from '@tanstack/react-query'
import { PlayIcon } from 'lucide-react'
import { useMemo, useState } from 'react'
import type { components } from '@/api/generated'
import { CORE_ORDER, DASH, fmt } from '@/lib/format'
import { useRefetchOn } from '@/lib/ws'
import { Correlation } from '@/screens/alpha-table/columns'
import { AlphaPane } from '@/screens/tasks/alpha-pane'
import { labTasks, type RankedAlpha } from '@/screens/tasks/api'
import { TASK_OFFER } from '@/screens/tasks/columns'
import { Button, ErrorNotice, Panel } from '@/ui/kit'
import { type Column, DataTable, type Sort } from '@/ui/table'

type SubmissionPick = components['schemas']['SubmissionPick']

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
  const rows = useMemo<RankedAlpha[]>(() => query.data ?? [], [query.data])

  return (
    <div className="flex flex-col gap-4">
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
