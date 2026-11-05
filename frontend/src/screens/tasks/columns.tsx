/** A task's status, and the metrics its across-regions summary compares. */

import type { ComponentProps } from 'react'
import { cn } from '@/lib/cn'
import { CORE_METRICS, CORE_ORDER, fmt } from '@/lib/format'
import type { LabTask, RankedAlpha, TaskStatus } from '@/screens/tasks/api'
import { type Badge, figureTone, type Limits, TEXT_TONE, type Tone } from '@/ui/kit'

export const setting = (r: RankedAlpha, key: string) => String(r.settings?.[key] ?? '')

const STATUS: Record<TaskStatus, { label: string; tone: ComponentProps<typeof Badge>['tone'] }> = {
  IDLE: { label: 'Not Started', tone: 'outline' },
  QUEUED: { label: 'Waiting', tone: 'warn' },
  RUNNING: { label: 'Running', tone: 'profit' },
  PAUSED: { label: 'Paused', tone: 'muted' },
  COMPLETE: { label: 'Complete', tone: 'neutral' },
  FAILED: { label: 'Failed', tone: 'loss' },
}

/** A task's status in words, wherever it is shown. */
export function taskStatus(task: LabTask) {
  if (task.stopping && task.status === 'RUNNING')
    return { label: 'Stopping', tone: 'warn' as const }
  // A stop ends the task COMPLETE; "Complete" beside 0 / 626 would claim work never done.
  if (task.stopping && task.status === 'COMPLETE' && task.simulated < task.target)
    return { label: 'Stopped', tone: 'muted' as const }
  return STATUS[task.status] ?? STATUS.IDLE
}

/** After-Cost Sharpe's header, which says on hover what the cost is. */
const AFTER_COST_HEADER = (
  <span title="The Sharpe after 5 bps of trading cost, charged on each day's own turnover.">
    After-Cost Sharpe
  </span>
)

/** The Alpha table's columns a task's Alphas have. */
export const TASK_OFFER = [
  'alphaId',
  'trial',
  'expression',
  'failedChecks',
  'region',
  'delay',
  'universe',
  'investability',
  'neutralization',
  'decay',
  'truncation',
  ...CORE_ORDER,
  'testSharpe',
  'testFitness',
  'afterCostSharpe',
  'longCount',
  'shortCount',
  'imbalance',
  'prodCorrelation',
  'powerPoolCorrelation',
]

export interface Metric {
  key: keyof RankedAlpha
  label: string
  show: (v: number | null) => string
  /** How the figure is coloured, given the limits its Alpha's checks set, if one Alpha's. */
  tone: (v: number | null, limits?: Limits) => Tone
  /** Which end of the range is the good one, for the across-regions "best" columns. Less
   *  trading is better: every submittable Alpha already clears BRAIN's 1% turnover floor, so
   *  the smallest turnover among them is the cheapest to hold, not one that barely trades. */
  best: 'max' | 'min'
}

export const METRICS: Metric[] = [
  ...CORE_ORDER.map(
    (key): Metric => ({
      key,
      ...CORE_METRICS[key],
      tone: (v, limits) => figureTone(key, v, limits),
      best: key === 'turnover' || key === 'drawdown' ? 'min' : 'max',
    }),
  ),
  {
    key: 'afterCostSharpe',
    label: 'After-Cost Sharpe',
    show: (v) => fmt.ratio(v),
    tone: (v, limits) => figureTone('sharpe', v, limits),
    best: 'max',
  },
  // The held-out years: an Alpha that decays shows it here, beside its full-period figures.
  {
    key: 'testSharpe',
    label: 'Test Sharpe',
    show: (v) => fmt.ratio(v),
    tone: (v, limits) => figureTone('sharpe', v, limits),
    best: 'max',
  },
  {
    key: 'testFitness',
    label: 'Test Fitness',
    show: (v) => fmt.ratio(v),
    tone: (v, limits) => figureTone('fitness', v, limits),
    best: 'max',
  },
]

/** A metric's figure, coloured as `figureTone` judges it: against `limits` when the figure
 *  is one Alpha's, and with none, only a negative stands out. */
export function Figure({
  metric,
  value,
  limits,
}: {
  metric: Metric
  value: number | null
  limits?: Limits
}) {
  return (
    <span className={cn('num', TEXT_TONE[metric.tone(value, limits)])}>{metric.show(value)}</span>
  )
}

export const metricHeader = (m: Metric) =>
  m.key === 'afterCostSharpe' ? AFTER_COST_HEADER : m.label
