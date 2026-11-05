/**
 * Getting Started: what a new consultant sets up before anything else, ticked off from real
 * state (never a stored flag) and hidden once all is done. Every open step is one click away.
 */

import { Link } from '@tanstack/react-router'
import { CheckIcon } from 'lucide-react'
import type { ReactNode } from 'react'
import type { Today } from '@/api/types'
import { cn } from '@/lib/cn'
import { useRunnableModels } from '@/screens/ai/shared'
import { Button, Panel, Progress } from '@/ui/kit'

type Variant = 'primary' | 'secondary'

interface Step {
  key: string
  title: string
  done: boolean
  action: (variant: Variant) => ReactNode
}

export function GettingStarted({ today }: { today: Today | undefined }) {
  const models = useRunnableModels()
  // Nothing while pending: returning users (setup done) would otherwise see a skeleton collapse.
  if (!today) return null

  const steps: Step[] = [
    {
      key: 'data',
      title: 'Sync BRAIN Datasets',
      done: today.catalog.anySynced,
      action: (variant) => (
        <Button size="sm" variant={variant} render={<Link to="/sync" />}>
          Sync
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
              <span
                className={cn(
                  'min-w-0 flex-1 text-body',
                  step.done ? 'text-ink-subtle line-through decoration-ink-tertiary' : 'text-ink',
                )}
              >
                {step.title}
              </span>
              {!step.done && step.action(isNext ? 'primary' : 'secondary')}
            </li>
          )
        })}
      </ol>
    </Panel>
  )
}
