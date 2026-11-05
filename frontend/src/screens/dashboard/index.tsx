/**
 * Dashboard (spec §4.1): the day in one sentence, the Submittable Alphas counter, today's
 * allowance and queue, and the work in flight. The live matrix has its own screen.
 */

import { useQuery } from '@tanstack/react-query'
import { AwardIcon, CpuIcon, LayersIcon, SendIcon } from 'lucide-react'
import type { ReactNode } from 'react'
import { today } from '@/api/core'
import type { components } from '@/api/generated'
import { http } from '@/api/http'
import { cn } from '@/lib/cn'
import { fmt } from '@/lib/format'

type QuarterStanding = components['schemas']['QuarterStanding']

import { useNow } from '@/lib/now'
import { useRefetchOn } from '@/lib/ws'
import { ErrorNotice, Page, Panel, Progress, Skeleton, TEXT_TONE, type Tone } from '@/ui/kit'
import { GettingStarted } from './getting-started'
import { WorkInFlight } from './work'

const DAY_SECONDS = 24 * 60 * 60

export function DashboardScreen() {
  const day = useQuery({ queryKey: ['today'], queryFn: () => today.get() })
  useRefetchOn('simulations', ['today'], 10_000)
  const sims = day.data?.simulations

  // The quarter BRAIN judges on: submitted Alphas and the pyramids they formed. It moves
  // only when something is submitted, which is rare, so it is not worth polling hard.
  const quarter = useQuery({
    queryKey: ['quarter'],
    queryFn: () => http.get<QuarterStanding>('/api/quarter'),
    staleTime: 10 * 60 * 1000,
  })

  return (
    <Page>
      {sims ? (
        <Hero name={day.data?.you.fullName} text={sims.headline} />
      ) : (
        !day.isError && (
          <Skeleton className="mx-1 mt-2 h-18 w-2/3" label="Loading today's figures" />
        )
      )}
      <GettingStarted today={day.data} />
      {day.isError && <ErrorNotice error={day.error} title="Today's figures could not load" />}

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          icon={<AwardIcon />}
          label="Submitted Alphas"
          loading={quarter.isPending}
          value={quarter.isError ? '—' : fmt.int(quarter.data?.submitted)}
          tone={quarter.data?.submitted ? 'profit' : 'neutral'}
          hint={
            quarter.isError
              ? 'The count could not load.'
              : `Submitted in ${quarter.data?.label ?? 'this quarter'}`
          }
        />
        <StatTile
          icon={<CpuIcon />}
          label="Simulations Left Today"
          loading={!sims}
          value={
            sims && (
              <>
                {sims.exact ? '' : '~'}
                {fmt.int(sims.remaining)}
                <span className="text-body text-ink-subtle"> / {fmt.int(sims.limit)}</span>
              </>
            )
          }
          extra={
            sims && (
              <Progress
                value={sims.limit ? sims.used / sims.limit : 0}
                label="Simulation quota depletion"
                // A fill left of the mark is allowance falling behind the day.
                mark={{
                  at: 1 - sims.resetsInSeconds / DAY_SECONDS,
                  label: `${fmt.pct(1 - sims.resetsInSeconds / DAY_SECONDS, 0)} of the quota day gone`,
                }}
              />
            )
          }
          hint={
            sims && (
              <>
                Resets in{' '}
                <ResetCountdown seconds={sims.resetsInSeconds} since={day.dataUpdatedAt} />
                {sims.exact ? '' : ' · estimate until the first result today'}
              </>
            )
          }
        />
        <StatTile
          icon={<SendIcon />}
          label="Sent Today"
          loading={!sims}
          value={sims && fmt.int(sims.used)}
          hint={
            sims && (
              <>
                <span className="num">
                  {fmt.pct(sims.limit ? sims.used / sims.limit : null, 0)}
                </span>{' '}
                of the allowance
              </>
            )
          }
        />
        <StatTile
          icon={<LayersIcon />}
          label="Pyramids Completed"
          loading={quarter.isPending}
          value={quarter.isError ? '—' : fmt.int(quarter.data?.pyramidsFormulated)}
          hint={
            quarter.isError ? (
              'The count could not load.'
            ) : (
              <>
                <span className="num">{fmt.int(quarter.data?.alphasPerPyramid)}</span> submitted
                Alphas complete one
                {quarter.data?.pyramidsStarted ? (
                  <>
                    {' · '}
                    <span className="num">{fmt.int(quarter.data.pyramidsStarted)}</span> started
                  </>
                ) : null}
              </>
            )
          }
        />
      </div>

      <WorkInFlight />
    </Page>
  )
}

/**
 * The day in one sentence, as the page's heading, greeted by the account's name from
 * `/api/today` (BRAIN profile, falling back to the email). Figures keep tabular digits.
 */
function Hero({ name, text }: { name?: string | null | undefined; text: string }) {
  const parts = text.split(/(~?\d[\d,.]*%?)/)
  return (
    <header className="flex flex-col px-1 pt-2 pb-1">
      {name && <p className="text-display text-ink">Welcome, {name}</p>}
      {/* text-wrap beats the base h1 balance rule, which splits this into an extra short line. */}
      <h1 className="text-display text-wrap text-ink-muted">
        {parts.map((part, i) =>
          i % 2 === 1 ? (
            <span key={i} className="num text-display text-primary">
              {part}
            </span>
          ) : (
            part
          ),
        )}
      </h1>
    </header>
  )
}

/** One layout for every tile: icon + label on top, the figure, then a hint. */
function StatTile({
  icon,
  label,
  value,
  hint,
  tone = 'neutral',
  loading,
  extra,
}: {
  icon?: ReactNode
  label: string
  value: ReactNode
  hint?: ReactNode
  tone?: Tone
  loading: boolean
  extra?: ReactNode
}) {
  return (
    <Panel bodyClassName="flex h-full flex-col justify-between gap-3">
      <div className="flex min-h-5 items-center gap-2">
        {icon && <span className="text-ink-subtle [&_svg]:size-3.5">{icon}</span>}
        <span className="text-caption font-medium uppercase tracking-wide text-ink-subtle">
          {label}
        </span>
      </div>
      {loading ? (
        <div className="flex flex-col gap-2">
          <Skeleton className="h-6 w-28" />
          <Skeleton className="h-4 w-40" />
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          <span className={cn('num text-headline font-semibold leading-none', TEXT_TONE[tone])}>
            {value}
          </span>
          {extra}
          {hint && <span className="text-body-compact text-ink-subtle">{hint}</span>}
        </div>
      )}
    </Panel>
  )
}

/** Ticks locally between refetches, in its own component so the page does not re-render. */
function ResetCountdown({ seconds, since }: { seconds: number; since: number }) {
  const now = useNow(1000)
  return <span className="num">{fmt.countdown(seconds - Math.max(0, (now - since) / 1000))}</span>
}
