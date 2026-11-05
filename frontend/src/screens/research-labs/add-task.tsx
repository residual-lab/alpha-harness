/**
 * The one way every lab and tool adds its task: start it now, or keep it in Tasks for later.
 * Both are always offered, named for what they do, so nobody has to guess which one runs.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from '@tanstack/react-router'
import { ListPlusIcon, PlayIcon } from 'lucide-react'
import { useId } from 'react'
import { toast } from 'sonner'
import type { components } from '@/api/generated'
import { errorMessage } from '@/api/http'
import { labTasks } from '@/screens/tasks/api'
import { Button } from '@/ui/kit'
import { Tooltip } from '@/ui/overlay'

type AddedTask = components['schemas']['AddedTask']

/** Added, but the start that followed was refused: the task is in Tasks, not running. */
class NotStarted extends Error {}

/** `mutate(true)` adds and starts the task; `mutate(false)` only adds it. */
export function useAddTask(add: () => Promise<AddedTask>) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const openTasks = { label: 'Open Tasks', onClick: () => void navigate({ to: '/tasks' }) }
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['lab-tasks'] })
    void queryClient.invalidateQueries({ queryKey: ['today'] })
  }
  return useMutation({
    mutationFn: async (run: boolean) => {
      const task = await add()
      if (run) {
        try {
          await labTasks.run(task.id)
        } catch (error) {
          throw new NotStarted(errorMessage(error))
        }
      }
      return task
    },
    onSuccess: (_task, run) => {
      refresh()
      toast.success(run ? 'Task Started' : 'Added to Tasks', {
        description: run
          ? 'It runs as soon as its cores are free. Follow it in Tasks.'
          : "Not started yet. Press Run on it in Tasks when you're ready.",
        action: openTasks,
      })
    },
    onError: (error) => {
      if (error instanceof NotStarted) {
        refresh()
        toast.error('Added to Tasks, but it did not start', {
          description: `${error.message} Start it from Tasks.`,
          action: openTasks,
        })
        return
      }
      toast.error('The task was not added', { description: errorMessage(error) })
    },
  })
}

/** Add to Tasks beside Run Now, in every lab and tool. */
export function AddTaskButtons({
  add,
  disabled,
  blocked,
  describedBy,
}: {
  add: ReturnType<typeof useAddTask>
  disabled: boolean
  /** Why both are disabled, said beside them: the input missing may be far down the page. */
  blocked?: string | null | undefined
  /** An element elsewhere saying why both are disabled, when they are. */
  describedBy?: string | undefined
}) {
  const running = add.isPending && add.variables
  const adding = add.isPending && !add.variables
  const reason = useId()
  const why = disabled && blocked
  const described = why ? reason : describedBy
  return (
    <div className="flex flex-wrap items-center gap-2">
      {why && (
        <span id={reason} className="text-body-compact text-ink-subtle">
          {why}
        </span>
      )}
      <Tooltip content="Saves it to Tasks without starting it. Run it from there when you're ready.">
        <Button
          disabled={disabled || running}
          loading={adding}
          aria-describedby={described}
          onClick={() => add.mutate(false)}
        >
          <ListPlusIcon />
          Add to Tasks
        </Button>
      </Tooltip>
      <Tooltip content="Adds it to Tasks and starts it as soon as its cores are free.">
        <Button
          variant="primary"
          disabled={disabled || adding}
          loading={running}
          aria-describedby={described}
          onClick={() => add.mutate(true)}
        >
          <PlayIcon />
          Run Now
        </Button>
      </Tooltip>
    </div>
  )
}
