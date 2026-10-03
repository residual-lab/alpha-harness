/**
 * Everything about one task, and what can be done with it. A task is never stopped: it pauses,
 * carries on from where it left off, or is cloned fresh, with another prompt for the LLM lab.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CopyIcon, PauseIcon, PlayIcon, Trash2Icon } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'
import { DASH, fmt } from '@/lib/format'
import { PromptPicker } from '@/screens/prompts/picker'
import { MAX_SIMULATIONS } from '@/screens/research-labs/lab-task'
import { type LabTask, labTasks, type TaskInfo } from '@/screens/tasks/api'
import { HoldButton } from '@/ui/hold-button'
import {
  Button,
  Checkbox,
  Disclosure,
  ErrorNotice,
  Field,
  Fieldset,
  Input,
  KV,
  Segmented,
  Skeleton,
} from '@/ui/kit'
import { Dialog } from '@/ui/overlay'
import { type Column, DataTable } from '@/ui/table'

/** Matches `labs.params.POWER_POOL_SAMPLER` and the prompt kind that lab sends. */
const POWER_POOL = 'power-pool'
const PROMPT_KIND = 'power_pool_lab'
/** Region Agnostic Lab: an LLM lab too, with a prompt of its own kind. */
const REGION_AGNOSTIC = 'region-agnostic'
const LLM_LABS: Record<string, string> = {
  [POWER_POOL]: PROMPT_KIND,
  [REGION_AGNOSTIC]: 'region_agnostic_lab',
}
/** Labs whose simulations are written when the task is added. */
const WRITTEN_UP_FRONT = new Set(['settings-sampler', 'correlation-breaker'])

export const useTaskInfo = (id: number) =>
  useQuery({ queryKey: ['lab-tasks', 'info', id], queryFn: () => labTasks.info(id) })

function useRefresh() {
  const queryClient = useQueryClient()
  return () => {
    for (const key of [['lab-tasks'], ['bar'], ['today'], ['simulations']])
      void queryClient.invalidateQueries({ queryKey: key })
  }
}

// ── Controls ────────────────────────────────────────────────────────────────────────────

/** Resume or pause, continue, clone, and a held Delete: the whole life of a task. */
export function TaskControls({
  task,
  onCloned,
  onDeleted,
}: {
  task: LabTask
  onCloned: (id: number) => void
  onDeleted: () => void
}) {
  const refresh = useRefresh()
  const [dialog, setDialog] = useState<'continue' | 'clone' | null>(null)
  const running = task.status === 'RUNNING' || task.status === 'QUEUED'
  const act = useMutation({
    mutationFn: (what: 'run' | 'pause') =>
      what === 'run' ? labTasks.run(task.id) : labTasks.pause(task.id),
    onSuccess: refresh,
  })
  const remove = useMutation({
    mutationFn: () => labTasks.remove(task.id, true),
    onSuccess: () => {
      toast.success('Task deleted', { description: 'The Alphas it found stay in Alphas.' })
      refresh()
      onDeleted()
    },
  })
  const resumable = task.status === 'IDLE' || task.status === 'PAUSED' || task.status === 'FAILED'

  return (
    <div className="flex flex-wrap items-center gap-2">
      {running ? (
        <Button size="sm" loading={act.isPending} onClick={() => act.mutate('pause')}>
          <PauseIcon />
          Pause
        </Button>
      ) : resumable ? (
        <Button
          size="sm"
          variant="primary"
          loading={act.isPending}
          onClick={() => act.mutate('run')}
        >
          <PlayIcon />
          {task.status === 'IDLE' ? 'Run' : 'Resume'}
        </Button>
      ) : null}
      {!running && (
        <Button size="sm" onClick={() => setDialog('continue')}>
          <PlayIcon />
          Continue…
        </Button>
      )}
      <Button size="sm" onClick={() => setDialog('clone')}>
        <CopyIcon />
        Clone…
      </Button>
      <HoldButton label="Delete task" pending={remove.isPending} onHold={() => remove.mutate()}>
        <Trash2Icon className="size-3.5" />
        Hold to Delete
      </HoldButton>
      {dialog === 'continue' && <ContinueDialog task={task} onClose={() => setDialog(null)} />}
      {dialog === 'clone' && (
        <CloneDialog
          task={task}
          onClose={() => setDialog(null)}
          onCloned={(id) => {
            setDialog(null)
            onCloned(id)
          }}
        />
      )}
    </div>
  )
}

function ContinueDialog({ task, onClose }: { task: LabTask; onClose: () => void }) {
  const refresh = useRefresh()
  const upFront = WRITTEN_UP_FRONT.has(task.lab)
  const met = task.simulated >= task.target
  const [more, setMore] = useState(String(met && !upFront ? task.target : 0))
  const count = Number(more)
  const valid =
    Number.isInteger(count) &&
    count >= 0 &&
    task.target + count <= MAX_SIMULATIONS &&
    (upFront || count > 0 || !met)
  const go = useMutation({
    meta: { inline: true },
    mutationFn: () => labTasks.continue(task.id, upFront ? 0 : count),
    onSuccess: () => {
      toast.success('Task continues', { description: 'It waits for its cores, then runs on.' })
      refresh()
      onClose()
    },
  })
  return (
    <Dialog
      open
      onOpenChange={(open) => !open && onClose()}
      title="Continue this task"
      description="From where it left off: its Alphas and what it learnt stay."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            disabled={!valid}
            loading={go.isPending}
            onClick={() => go.mutate()}
          >
            Continue
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <p className="text-body text-ink-muted">
          It has simulated <span className="num text-ink">{fmt.int(task.simulated)}</span> of{' '}
          <span className="num text-ink">{fmt.int(task.target)}</span>.
        </p>
        {upFront ? (
          <p className="text-body-compact text-pretty text-ink-subtle">
            Its simulations were written when it was added, so it sends what is left of them. Clone
            it to run the whole set again.
          </p>
        ) : (
          <Field
            label="Simulations to add"
            hint={
              met
                ? 'It met its target, so it needs more to carry on.'
                : 'Zero carries on towards the target it has.'
            }
          >
            <Input
              type="number"
              min={0}
              max={MAX_SIMULATIONS - task.target}
              step={1}
              className="w-40"
              value={more}
              onChange={(e) => setMore(e.target.value)}
            />
          </Field>
        )}
        {go.isError && <ErrorNotice error={go.error} title="Could not continue it" />}
      </div>
    </Dialog>
  )
}

function CloneDialog({
  task,
  onClose,
  onCloned,
}: {
  task: LabTask
  onClose: () => void
  onCloned: (id: number) => void
}) {
  const refresh = useRefresh()
  const llm = task.lab in LLM_LABS
  const upFront = WRITTEN_UP_FRONT.has(task.lab)
  const [simulations, setSimulations] = useState(String(task.target))
  const [prompt, setPrompt] = useState<'same' | 'other'>('same')
  const [promptId, setPromptId] = useState<number | null>(null)
  const [run, setRun] = useState(false)
  const count = Number(simulations)
  const valid = upFront || (Number.isInteger(count) && count >= 1 && count <= MAX_SIMULATIONS)
  const go = useMutation({
    meta: { inline: true },
    mutationFn: () =>
      labTasks.clone(task.id, {
        simulations: upFront ? null : count,
        change_prompt: llm && prompt === 'other',
        prompt_id: llm && prompt === 'other' ? promptId : null,
        run,
      }),
    onSuccess: (added) => {
      toast.success(`Cloned as task ${added.id}`, {
        description: run ? 'It waits for its cores, then runs.' : 'Run it when you are ready.',
      })
      refresh()
      onCloned(added.id)
    },
  })
  return (
    <Dialog
      open
      onOpenChange={(open) => !open && onClose()}
      title="Clone this task"
      description="A fresh task with the same market, datasets, fields and settings, and nothing it did."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            disabled={!valid}
            loading={go.isPending}
            onClick={() => go.mutate()}
          >
            <CopyIcon />
            Clone
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        {!upFront && (
          <Field label="Simulations">
            <Input
              type="number"
              min={1}
              max={MAX_SIMULATIONS}
              step={1}
              className="w-40"
              value={simulations}
              onChange={(e) => setSimulations(e.target.value)}
            />
          </Field>
        )}
        {llm && (
          <Fieldset legend="Prompt" hint={`This task sends: ${task.promptName ?? 'Built-in'}.`}>
            <Segmented
              label="Prompt"
              items={[
                { value: 'same', label: 'Keep its prompt' },
                { value: 'other', label: 'Choose another' },
              ]}
              value={prompt}
              onChange={setPrompt}
            />
            {prompt === 'other' && (
              <PromptPicker
                kind={LLM_LABS[task.lab] ?? PROMPT_KIND}
                value={promptId}
                onChange={setPromptId}
              />
            )}
          </Fieldset>
        )}
        <Checkbox label="Run it now" checked={run} onChange={setRun} />
        {go.isError && <ErrorNotice error={go.error} title="Could not clone it" />}
      </div>
    </Dialog>
  )
}

// ── Everything known ────────────────────────────────────────────────────────────────────

const DATASET_COLUMNS: Column<TaskInfo['datasets'][number]>[] = [
  { key: 'name', header: 'Dataset', width: 'minmax(180px,2fr)', cell: (d) => d.name },
  {
    key: 'id',
    header: 'Id',
    width: 'minmax(120px,1fr)',
    cell: (d) => <span className="num truncate text-ink-muted">{d.id}</span>,
  },
  {
    key: 'simulated',
    header: 'Simulated',
    width: '100px',
    align: 'right',
    cell: (d) => fmt.int(d.simulated),
  },
  {
    key: 'complete',
    header: 'Complete',
    width: '96px',
    align: 'right',
    cell: (d) => fmt.int(d.complete),
  },
  {
    key: 'failed',
    header: 'Failed',
    width: '80px',
    align: 'right',
    cell: (d) => fmt.int(d.failed),
  },
  { key: 'best', header: 'Best', width: '80px', align: 'right', cell: (d) => fmt.ratio(d.best) },
]

const CALL_COLUMNS: Column<TaskInfo['recentCalls'][number]>[] = [
  { key: 'at', header: 'Time', width: '150px', cell: (c) => fmt.dateTime(c.at) },
  {
    key: 'dataset',
    header: 'Dataset',
    width: 'minmax(110px,1fr)',
    cell: (c) => <span className="num truncate">{c.dataset ?? DASH}</span>,
  },
  { key: 'prompt', header: 'Prompt', width: 'minmax(110px,1fr)', cell: (c) => c.prompt ?? DASH },
  { key: 'model', header: 'Model', width: 'minmax(110px,1fr)', cell: (c) => c.model ?? DASH },
  {
    key: 'fields',
    header: 'Fields',
    width: '64px',
    align: 'right',
    cell: (c) => fmt.int(c.fields),
  },
  { key: 'valid', header: 'Valid', width: '64px', align: 'right', cell: (c) => fmt.int(c.valid) },
  {
    key: 'rejected',
    header: 'Rejected',
    width: '80px',
    align: 'right',
    cell: (c) => fmt.int(c.rejected),
  },
  {
    key: 'tokens',
    header: 'Tokens',
    width: '80px',
    align: 'right',
    cell: (c) => fmt.int(c.tokens),
  },
  {
    key: 'error',
    header: 'Error',
    width: 'minmax(140px,2fr)',
    cell: (c) =>
      c.error ? (
        <span className="truncate text-pnl-negative" title={c.error}>
          {c.error}
        </span>
      ) : (
        DASH
      ),
  },
]

/** How it was set up, when things happened, where its simulations went and what it asked. */
export function TaskAbout({ task }: { task: LabTask }) {
  const info = useTaskInfo(task.id)
  if (info.isError) return <ErrorNotice error={info.error} title="Could not load this task" />
  if (!info.data) return <Skeleton className="h-40" />
  const d = info.data
  return (
    <Disclosure summary="Everything about this task">
      <div className="flex flex-col gap-5">
        <div className="grid grid-cols-1 gap-x-8 gap-y-4 lg:grid-cols-2">
          <section aria-label="When" className="flex flex-col gap-2">
            <h3 className="text-caption font-medium text-ink-muted">When</h3>
            <KV
              items={[
                ['Added', fmt.dateTime(d.createdAt)],
                ['Run pressed', fmt.dateTime(d.queuedAt)],
                ['Started', fmt.dateTime(d.startedAt)],
                [task.status === 'PAUSED' ? 'Paused' : 'Finished', fmt.dateTime(d.finishedAt)],
                ['Last change', fmt.dateTime(d.updatedAt)],
              ]}
            />
          </section>
          <section aria-label="Setup" className="flex flex-col gap-2">
            <h3 className="text-caption font-medium text-ink-muted">Setup</h3>
            <KV items={d.settings.map((s) => [s.label, s.value])} />
          </section>
        </div>

        {d.datasets.length > 0 && (
          <section aria-label="Datasets simulated" className="flex flex-col gap-2">
            <h3 className="text-caption font-medium text-ink-muted">Datasets simulated</h3>
            <DataTable
              label="Datasets simulated"
              rows={d.datasets}
              columns={DATASET_COLUMNS}
              rowKey={(r) => r.id}
              maxHeight="40vh"
            />
          </section>
        )}

        {d.fields.length > 0 && (
          <section aria-label="Chosen fields" className="flex flex-col gap-2">
            <h3 className="text-caption font-medium text-ink-muted">
              {fmt.int(d.fields.length)} chosen fields
              {d.rankBy ? `, ranked by ${d.rankBy}` : ''}
            </h3>
            <ol className="flex max-h-40 flex-wrap gap-1.5 overflow-auto">
              {d.fields.map((f, i) => (
                <li
                  key={f}
                  className="inline-flex h-7 items-center gap-1 rounded-sm border border-hairline-strong bg-surface-3 px-2 text-body-compact"
                >
                  <span className="num text-ink-subtle">{i + 1}</span>
                  <span className="num text-ink">{f}</span>
                </li>
              ))}
            </ol>
          </section>
        )}

        {task.lab in LLM_LABS && (
          <section aria-label="LLM calls" className="flex flex-col gap-2">
            <h3 className="text-caption font-medium text-ink-muted">
              LLM calls · <span className="num">{fmt.int(d.llmCalls)}</span> made, latest first
            </h3>
            <DataTable
              label="LLM calls"
              rows={d.recentCalls}
              columns={CALL_COLUMNS}
              rowKey={(c) => `${c.at}-${c.dataset}`}
              maxHeight="40vh"
              empty="No LLM calls yet."
            />
          </section>
        )}
      </div>
    </Disclosure>
  )
}
