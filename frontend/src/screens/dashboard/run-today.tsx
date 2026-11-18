/**
 * The Dashboard's one click: a focused run of today's simulations, now, with nothing to choose.
 * It builds on the best Top pick and searches the pyramids not yet formulated; the full
 * allowance is one disclosure away.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { PlayIcon } from 'lucide-react'
import { useState } from 'react'
import { today as todayApi } from '@/api/core'
import type { Today } from '@/api/types'
import { fmt } from '@/lib/format'
import { Button, Disclosure, ErrorNotice, Input, LINK, Notice, Panel } from '@/ui/kit'

/** The backend's DEFAULT_RUN: about half an hour over eight cores. */
const DEFAULT_RUN = 500

export function RunToday({ today }: { today: Today | undefined }) {
  const queryClient = useQueryClient()
  // The typed text, not the number sent: binding the field to the clamped value would rewrite
  // it mid-keystroke.
  const [typed, setTyped] = useState<string | null>(null)
  const run = useMutation({
    mutationFn: (simulations: number) => todayApi.run(simulations),
    // Said below the button, not in a toast as well.
    meta: { inline: true },
    onSuccess: () => {
      for (const queryKey of [['simulations'], ['bar'], ['today'], ['lab-tasks']])
        void queryClient.invalidateQueries({ queryKey })
    },
  })

  // Getting Started owns the one action until something is synced.
  if (!today?.catalog.anySynced) return null
  const left = today.simulations.unspoken
  const typedNumber = typed === null ? null : Number.parseInt(typed, 10)
  const size = Math.max(
    1,
    Math.min(
      left,
      typedNumber !== null && Number.isFinite(typedNumber) ? typedNumber : DEFAULT_RUN,
    ),
  )

  return (
    <section aria-label="Today's simulations" className="flex flex-col gap-2">
      {left > 0 && !run.isSuccess && (
        <Panel bodyClassName="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-3">
            <Button
              variant="primary"
              className="max-sm:w-full"
              loading={run.isPending}
              onClick={() => run.mutate(size)}
            >
              <PlayIcon />
              Run {fmt.int(size)} Simulations
            </Button>
            <span className="text-body-compact text-ink-subtle">
              Builds on your best Top pick, then searches pyramids not yet formulated this quarter.
            </span>
          </div>
          <Disclosure summary="Run more">
            <label className="flex flex-wrap items-center gap-2 text-body text-ink-subtle">
              Simulations
              <Input
                type="number"
                min={1}
                max={left}
                value={typed ?? String(size)}
                onChange={(e) => setTyped(e.currentTarget.value)}
                // Held to what the day allows once the field is left, never mid-keystroke.
                onBlur={() => setTyped(String(size))}
                className="w-28"
              />
              of <span className="num">{fmt.int(left)}</span> left today. The full allowance takes
              hours; sleep pauses it and nothing is lost.
            </label>
          </Disclosure>
        </Panel>
      )}
      {/* Always mounted, so the outcome is announced when it arrives. */}
      <div aria-live="polite" className="flex flex-col gap-2">
        {run.isSuccess && (
          <>
            <p className="text-body text-ink">
              Started <span className="num">{fmt.int(run.data.tasks.length)}</span>{' '}
              {run.data.tasks.length === 1 ? 'task' : 'tasks'} · up to{' '}
              <span className="num">{fmt.int(run.data.simulations)}</span> simulations. They run as
              their cores free up.{' '}
              <Link to="/tasks" className={LINK}>
                Open Tasks
              </Link>
            </p>
            {run.data.notes.length > 0 && (
              <Notice>
                {run.data.notes.map((note) => (
                  <p key={note}>{note}</p>
                ))}
              </Notice>
            )}
          </>
        )}
        {run.isError && (
          <ErrorNotice error={run.error} title="Today's simulations could not start" />
        )}
      </div>
    </section>
  )
}
