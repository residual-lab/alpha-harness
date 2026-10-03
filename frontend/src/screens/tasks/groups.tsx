/**
 * Region Agnostic Lab's results: each Alpha once, with a row for its run in every region.
 * Runs come back at different times and land in their group as they do. Calibrate runs BRAIN's
 * submission checks on them, and the groups rank themselves most submittable first.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { GaugeIcon } from 'lucide-react'
import { toast } from 'sonner'
import { cn } from '@/lib/cn'
import { DASH, fmt } from '@/lib/format'
import { type AlphaGroup, type GroupAlpha, type LabTask, labTasks } from '@/screens/tasks/api'
import { Badge, Button, ErrorNotice, signTone, TEXT_TONE } from '@/ui/kit'
import { type Column, DataTable } from '@/ui/table'

/** A table row: one region's run, with its group's facts on the group's first row. */
type Row = GroupAlpha & { rank: number; group: AlphaGroup; first: boolean }

const STATE: Record<
  GroupAlpha['state'],
  { label: string; tone: 'profit' | 'warn' | 'loss' | 'muted' }
> = {
  complete: { label: 'Back', tone: 'profit' },
  running: { label: 'On BRAIN', tone: 'warn' },
  waiting: { label: 'Waiting', tone: 'muted' },
  failed: { label: 'Failed', tone: 'loss' },
}

const checks = (passed: number, failed: number, pending: number) => (
  <span className="num">
    <span className="text-pnl-positive">{fmt.int(passed)}</span>
    {' / '}
    <span className={failed ? 'text-pnl-negative' : 'text-ink-subtle'}>{fmt.int(failed)}</span>
    {pending > 0 && <span className="text-ink-subtle"> · {fmt.int(pending)}?</span>}
  </span>
)

const COLUMNS: Column<Row>[] = [
  {
    key: 'rank',
    header: '#',
    width: '48px',
    align: 'right',
    cell: (r) => (r.first ? fmt.int(r.rank) : ''),
  },
  {
    key: 'expression',
    header: 'Alpha',
    width: 'minmax(260px,3fr)',
    cell: (r) =>
      r.first ? (
        <span className="num truncate text-ink" title={r.group.expression ?? undefined}>
          {r.group.expression}
        </span>
      ) : (
        <span className="text-ink-tertiary">″</span>
      ),
  },
  {
    key: 'settings',
    header: 'Settings',
    width: 'minmax(130px,1fr)',
    cell: (r) =>
      r.first ? (
        <span className="num truncate text-ink-subtle">
          {r.group.neutralization ?? DASH} · D{r.group.decay ?? DASH}
        </span>
      ) : (
        ''
      ),
  },
  {
    key: 'group-checks',
    header: 'Group Checks',
    width: '112px',
    align: 'right',
    cell: (r) => (r.first ? checks(r.group.passed, r.group.failed, r.group.pending) : ''),
  },
  {
    key: 'region',
    header: 'Region',
    width: '72px',
    cell: (r) => <Badge tone="outline">{r.region ?? DASH}</Badge>,
  },
  {
    key: 'universe',
    header: 'Universe',
    width: '100px',
    cell: (r) => <span className="num truncate text-ink-muted">{r.universe ?? DASH}</span>,
  },
  {
    key: 'state',
    header: 'Status',
    width: '88px',
    cell: (r) => <Badge tone={STATE[r.state].tone}>{STATE[r.state].label}</Badge>,
  },
  {
    key: 'sharpe',
    header: 'Sharpe',
    width: '72px',
    align: 'right',
    cell: (r) => <span className={TEXT_TONE[signTone(r.sharpe)]}>{fmt.ratio(r.sharpe)}</span>,
  },
  {
    key: 'fitness',
    header: 'Fitness',
    width: '72px',
    align: 'right',
    cell: (r) => fmt.ratio(r.fitness),
  },
  {
    key: 'turnover',
    header: 'Turnover',
    width: '80px',
    align: 'right',
    cell: (r) => fmt.pct(r.turnover),
  },
  {
    key: 'checks',
    header: 'Checks Pass / Fail',
    width: '120px',
    align: 'right',
    cell: (r) => (r.state === 'complete' ? checks(r.passed, r.failed, r.pending) : DASH),
  },
  {
    key: 'failing',
    header: 'Failing',
    width: 'minmax(140px,1.4fr)',
    cell: (r) =>
      r.failing.length ? (
        <span className="num truncate text-pnl-negative" title={r.failing.join(', ')}>
          {r.failing.join(', ')}
        </span>
      ) : (
        ''
      ),
  },
]

export function AlphaGroups({
  task,
  onOpenAlpha,
}: {
  task: LabTask
  onOpenAlpha: (alphaId: string) => void
}) {
  const queryClient = useQueryClient()
  const groups = useQuery({
    queryKey: ['lab-tasks', 'groups', task.id],
    queryFn: () => labTasks.groups(task.id),
    // Fresh runs and calibrated checks land continuously while either is going on.
    refetchInterval: (q) => (q.state.data?.calibrating || task.status === 'RUNNING' ? 4000 : false),
  })
  const calibrate = useMutation({
    mutationFn: () => labTasks.calibrate(task.id),
    onSuccess: () => {
      toast.success('Calibrating', {
        description: 'BRAIN checks each Alpha in turn; the groups re-rank as answers land.',
      })
      void queryClient.invalidateQueries({ queryKey: ['lab-tasks', 'groups', task.id] })
    },
  })
  const data = groups.data
  const rows: Row[] = (data?.groups ?? []).flatMap((group, i) =>
    group.alphas.map((a, j) => ({ ...a, rank: i + 1, group, first: j === 0 })),
  )

  return (
    <section aria-label="Alpha groups" className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-3">
        <h3 className="text-title font-medium text-ink">Alpha groups</h3>
        {data && (
          <span className="num text-body-compact text-ink-subtle">
            {fmt.int(data.groups.length)} Alphas · {fmt.int(data.complete)} regional runs back ·{' '}
            {fmt.int(data.calibrated)} calibrated
          </span>
        )}
        <Button
          size="sm"
          variant="primary"
          className="ml-auto"
          loading={calibrate.isPending || data?.calibrating}
          disabled={!data?.complete}
          title="Run BRAIN's submission checks on every Alpha back. No quota is spent."
          onClick={() => calibrate.mutate()}
        >
          <GaugeIcon />
          {data?.calibrating ? 'Calibrating…' : 'Calibrate'}
        </Button>
      </div>
      <p className="text-body-compact text-pretty text-ink-subtle">
        Ranked by the share of submission checks passed across the group&apos;s regions, then most
        passed, then best Sharpe. Theme, cluster and pyramid checks are left out; ? marks checks
        BRAIN has not run yet, which Calibrate resolves.
      </p>
      {groups.isError && <ErrorNotice error={groups.error} title="Could not load the groups" />}
      {calibrate.isError && <ErrorNotice error={calibrate.error} title="Could not calibrate" />}
      <DataTable
        label="Alpha groups"
        rows={rows}
        columns={COLUMNS}
        rowKey={(r) => String(r.trialId)}
        rowClass={(r) => cn(r.first && 'border-t border-t-hairline-strong')}
        onRowClick={(r) => r.alphaId && onOpenAlpha(r.alphaId)}
        loading={groups.isPending}
        empty="No Alphas written yet."
        maxHeight="70vh"
      />
    </section>
  )
}
