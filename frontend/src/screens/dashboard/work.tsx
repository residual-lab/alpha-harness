/**
 * Work in flight: every task holding queued simulations or cores (work queued by labs),
 * and the background tasks summary.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { toast } from 'sonner'
import { simulations, tasks as tasksApi } from '@/api/core'
import { DASH, fmt } from '@/lib/format'
import { useLive } from '@/lib/live'
import { Badge, Button, Empty, ErrorNotice, Panel, Progress, Skeleton } from '@/ui/kit'
import { Confirm } from '@/ui/overlay'
import { type Column, DataTable } from '@/ui/table'

interface WorkRow {
  task: string
  running: boolean
  inFlight: number
  waiting: number
}

export function WorkInFlight() {
  const queryClient = useQueryClient()
  // The header's own query, which it already refreshes on every simulations message.
  const status = useQuery({
    queryKey: ['simulations', 'engine'],
    queryFn: () => simulations.engine(),
  })

  // `task` undefined = pause everything.
  const [confirm, setConfirm] = useState<{
    task?: string
    name: string
    waiting: number
  } | null>(null)
  const stop = useMutation({
    mutationFn: (task?: string) => simulations.dropQueue(task),
    onSuccess: (result, task) => {
      toast.success(
        `Paused ${task ?? 'every task'}: ${fmt.int(result.dropped)} queued simulations wait for Run on the Tasks screen. Anything already sent to BRAIN keeps running.`,
      )
      for (const queryKey of [['simulations'], ['bar'], ['today'], ['lab-tasks']])
        void queryClient.invalidateQueries({ queryKey })
    },
    onSettled: () => setConfirm(null),
  })

  const data = status.data
  const rows: WorkRow[] = data
    ? [...new Set([...Object.keys(data.queued), ...Object.keys(data.inFlight)])].map((task) => ({
        task,
        running: (data.inFlight[task] ?? 0) > 0,
        inFlight: data.inFlight[task] ?? 0,
        waiting: data.queued[task] ?? 0,
      }))
    : []
  const stoppable = rows.filter((r) => r.waiting > 0)
  const waitingTotal = stoppable.reduce((sum, r) => sum + r.waiting, 0)

  const columns: Column<WorkRow>[] = [
    {
      key: 'task',
      header: 'Task',
      width: 'minmax(220px,3fr)',
      cell: (r) => <span className="num truncate text-ink">{r.task}</span>,
    },
    {
      key: 'status',
      header: 'Status',
      width: '150px',
      cell: (r) => (
        <Badge tone={r.running ? 'neutral' : 'outline'}>{r.running ? 'Running' : 'Queued'}</Badge>
      ),
    },
    {
      key: 'inFlight',
      header: 'On BRAIN',
      align: 'right',
      width: '90px',
      cell: (r) => `${fmt.int(r.inFlight)} cores`,
    },
    {
      key: 'waiting',
      header: 'Waiting',
      align: 'right',
      width: '80px',
      cell: (r) => fmt.int(r.waiting),
    },
    {
      key: 'actions',
      header: <span className="sr-only">Actions</span>,
      width: '72px',
      align: 'right',
      cell: (r) =>
        r.waiting > 0 && (
          <Button
            variant="ghost"
            size="sm"
            disabled={stop.isPending}
            onClick={() => setConfirm({ task: r.task, name: r.task, waiting: r.waiting })}
          >
            Pause
          </Button>
        ),
    },
  ]

  return (
    <Panel
      title="Work in Flight"
      actions={
        stoppable.length > 0 && (
          <Button
            size="sm"
            disabled={stop.isPending}
            onClick={() => setConfirm({ name: 'every task', waiting: waitingTotal })}
          >
            Pause all
          </Button>
        )
      }
      bodyClassName="flex flex-col gap-4"
    >
      {status.isPending ? (
        <Skeleton className="h-24" label="Loading work in flight" />
      ) : status.isError ? (
        <ErrorNotice error={status.error} title="Queued work could not load" />
      ) : rows.length === 0 ? (
        <Empty title="Nothing is queued right now" />
      ) : (
        <DataTable
          label="Work in Flight"
          rows={rows}
          columns={columns}
          rowKey={(r) => r.task}
          rowHeight={48}
          maxHeight="24rem"
        />
      )}

      <TasksSummary />

      <Confirm
        open={confirm !== null}
        onOpenChange={(open) => !open && !stop.isPending && setConfirm(null)}
        title={confirm?.task ? `Pause ${confirm.name}?` : 'Pause every task?'}
        confirmLabel="Pause"
        cancelLabel="Keep going"
        pending={stop.isPending}
        onConfirm={() => stop.mutate(confirm?.task)}
      >
        Takes <span className="num text-ink">{fmt.int(confirm?.waiting)}</span> queued simulations
        of {confirm?.name} off the queue; they are sent when you run it again from Tasks. Anything
        already sent to BRAIN keeps running.
      </Confirm>
    </Panel>
  )
}

/** Background tasks (syncs, backfills, lab runs) from the socket, with a REST read until it lands. */
function TasksSummary() {
  const live = useLive((s) => s.tasks)
  const fallback = useQuery({
    queryKey: ['background-tasks'],
    queryFn: () => tasksApi.list(),
    enabled: live === null,
  })
  const summary = live ?? fallback.data

  if (live === null && fallback.isError)
    return <ErrorNotice error={fallback.error} title="Background tasks could not load" />
  if (!summary) return null
  const shown = summary.tasks.filter((t) => t.state !== 'done')

  return (
    <div className="flex flex-col gap-2 border-t border-hairline pt-3">
      <p className="text-body-compact text-ink-subtle">
        Background Tasks:{' '}
        <span className="num font-medium text-ink">{fmt.int(summary.running)}</span> running
        {summary.failed > 0 && (
          <>
            {' '}
            · <span className="num text-pnl-negative-text">{fmt.int(summary.failed)}</span> failed
          </>
        )}
      </p>
      {shown.map((t) => (
        <div key={t.id} className="flex flex-col gap-1 text-body">
          <div className="flex items-center gap-2">
            <span className="min-w-0 flex-1 truncate text-ink">{t.label}</span>
            <Badge
              tone={t.state === 'failed' ? 'loss' : t.state === 'cancelled' ? 'outline' : 'neutral'}
            >
              {t.state}
            </Badge>
            <span className="num text-body-compact text-ink-subtle">
              {t.progress == null ? DASH : fmt.pct(t.progress, 0)}
            </span>
          </div>
          {t.state === 'running' && <Progress value={t.progress} label={t.label} />}
          {(t.error || t.detail) && (
            <span
              className={
                t.error
                  ? 'text-body-compact text-pnl-negative-text'
                  : 'text-body-compact text-ink-subtle'
              }
            >
              {t.error ?? t.detail}
            </span>
          )}
        </div>
      ))}
    </div>
  )
}
