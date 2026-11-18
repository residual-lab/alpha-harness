/**
 * Getting Started: what a new consultant sets up before anything else, ticked off from real
 * state (never a stored flag) and hidden once all is done. Every open step is one click away.
 */

import { Link } from '@tanstack/react-router'
import { CheckIcon } from 'lucide-react'
import type { ReactNode } from 'react'
import type { SyncMarket, Today } from '@/api/types'
import { cn } from '@/lib/cn'
import { useLive } from '@/lib/live'
import { useRunnableModels } from '@/screens/ai/shared'
import { Button, Panel, Progress } from '@/ui/kit'

type Variant = 'primary' | 'secondary'

interface Step {
  key: string
  title: string
  done: boolean
  /** What an open step is waiting on, under its title. */
  detail?: ReactNode
  action: (variant: Variant) => ReactNode
}

/** A market in one of these has its fields stored: a task can already run there. */
const LANDED = new Set<SyncMarket['state']>(['fields', 'details', 'done'])

export function GettingStarted({ today }: { today: Today | undefined }) {
  const models = useRunnableModels()
  // The first sign-in starts the first sync by itself; its progress arrives on the socket.
  const live = useLive((s) => s.sync)
  // Nothing while pending: returning users (setup done) would otherwise see a skeleton collapse.
  if (!today) return null

  const sync = live ?? today.catalog.running
  const syncing = sync?.status === 'RUNNING'
  const steps: Step[] = [
    {
      key: 'data',
      title: 'Sync BRAIN Datasets',
      // Done the moment the first market lands, not when the whole catalog has.
      done: today.catalog.anySynced || Boolean(live?.markets?.some((m) => LANDED.has(m.state))),
      detail: syncing ? (
        'Downloading from BRAIN…'
      ) : sync?.status === 'FAILED' && sync.error ? (
        <span className="text-pnl-negative-text">The sync failed. {sync.error}</span>
      ) : undefined,
      action: (variant) => (
        <Button size="sm" variant={variant} render={<Link to="/sync" />}>
          {syncing ? 'View' : 'Sync'}
        </Button>
      ),
    },
    {
      key: 'ai',
      title: 'Add a Free API Key',
      done: today.assistant.enabledKeys > 0,
      action: (variant) => (
        <Button
          size="sm"
          variant={variant}
          render={<Link to="/ai/$tab" params={{ tab: 'providers' }} />}
        >
          Add a Key
        </Button>
      ),
    },
    {
      // A Key alone runs nothing: the Assistant and Power Pool Lab need a model on it.
      key: 'model',
      title: 'Set Up a Model',
      done: !models.ready || models.runnable.length > 0,
      action: (variant) => (
        <Button
          size="sm"
          variant={variant}
          render={<Link to="/ai/$tab" params={{ tab: 'models' }} />}
        >
          Set Up a Model
        </Button>
      ),
    },
  ]

  const done = steps.filter((s) => s.done).length
  if (done === steps.length) return null
  const next = steps.find((s) => !s.done)

  return (
    <Panel
      title="Getting Started"
      description={
        <>
          <span className="num">
            {done} of {steps.length}
          </span>{' '}
          done
        </>
      }
      actions={
        <Progress value={done / steps.length} className="w-32" label="Getting started progress" />
      }
      bodyClassName="p-0"
    >
      <ol className="divide-y divide-hairline">
        {steps.map((step, i) => {
          const isNext = step === next
          return (
            <li
              key={step.key}
              className={cn(
                'flex items-center gap-4 px-4 py-3 transition-colors',
                isNext && 'bg-surface-2',
              )}
            >
              <span
                className={cn(
                  'flex size-7 shrink-0 items-center justify-center rounded-xs border text-body-compact',
                  step.done
                    ? 'border-pnl-positive-edge bg-pnl-positive-tint text-pnl-positive-text'
                    : isNext
                      ? 'border-ink-subtle text-ink'
                      : 'border-hairline-strong text-ink-subtle',
                )}
                role="img"
                aria-label={step.done ? 'Done' : `Step ${i + 1}`}
              >
                {step.done ? (
                  <CheckIcon className="size-3.5" />
                ) : (
                  <span className="num">{i + 1}</span>
                )}
              </span>
              <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                <span
                  className={cn(
                    'text-body',
                    step.done ? 'text-ink-subtle line-through decoration-ink-tertiary' : 'text-ink',
                  )}
                >
                  {step.title}
                </span>
                {!step.done && step.detail && (
                  <span className="text-body-compact text-pretty break-words text-ink-subtle">
                    {step.detail}
                  </span>
                )}
              </span>
              {!step.done && step.action(isNext ? 'primary' : 'secondary')}
            </li>
          )
        })}
      </ol>
    </Panel>
  )
}
