/**
 * Competitions: what BRAIN is running now and where you stand. Joining and submitting both
 * happen on BRAIN.
 */

import { useQuery } from '@tanstack/react-query'
import { ExternalLinkIcon, TrophyIcon } from 'lucide-react'
import { type Competition, competitions } from '@/api/core'
import { PLATFORM_COMPETITIONS_URL } from '@/api/platform'
import { fmt } from '@/lib/format'
import {
  Badge,
  Button,
  Disclosure,
  Empty,
  ErrorNotice,
  Metric,
  Page,
  PageHeader,
  Panel,
  Skeleton,
} from '@/ui/kit'

const BRAIN_COMPETITION_URL = (id: string) =>
  `${PLATFORM_COMPETITIONS_URL}${encodeURIComponent(id)}`

const DAY_MS = 86_400_000

const daysLeft = (iso: string | null) =>
  iso ? Math.max(0, Math.ceil((new Date(iso).getTime() - Date.now()) / DAY_MS)) : null

function CompetitionCard({ competition: c }: { competition: Competition }) {
  const left = daysLeft(c.endDate)
  const signUpOpen = !c.enrolled && (daysLeft(c.signUpEndDate) ?? 0) > 0

  return (
    <Panel
      title={c.name}
      actions={
        <>
          <Badge tone={c.enrolled ? 'profit' : 'warn'}>
            {c.enrolled ? 'Enrolled' : 'Not enrolled'}
          </Badge>
          {c.faq && (
            <Button
              variant="ghost"
              size="sm"
              render={<a href={c.faq} target="_blank" rel="noopener noreferrer" />}
            >
              Rules
            </Button>
          )}
          <Button
            size="sm"
            render={
              <a href={BRAIN_COMPETITION_URL(c.id)} target="_blank" rel="noopener noreferrer" />
            }
          >
            <ExternalLinkIcon aria-hidden />
            Open in BRAIN
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-5">
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <Metric boxed label="Ends" value={fmt.date(c.endDate)} />
          <Metric boxed label="Days Left" value={left === null ? '—' : fmt.int(left)} />
          <Metric
            boxed
            label="Your Rank"
            value={c.standing?.rank ? `#${fmt.int(c.standing.rank)}` : '—'}
          />
          <Metric boxed label="Your Alphas" value={fmt.int(c.standing?.alphas)} />
        </div>
        {signUpOpen && (
          <p className="text-body text-ink-subtle">
            Sign up on BRAIN by {fmt.date(c.signUpEndDate)} for your Alphas to count. Alpha Harness
            never signs up for you.
          </p>
        )}
      </div>
    </Panel>
  )
}

export function CompetitionsScreen() {
  const list = useQuery({ queryKey: ['competitions'], queryFn: competitions.list })
  const all = list.data?.competitions ?? []
  const live = all.filter((c) => c.ongoing)
  const past = all.filter((c) => !c.ongoing)

  return (
    <Page>
      <PageHeader
        title="Competitions"
        description="What BRAIN is running now, and where you stand. Joining and submitting happen on BRAIN."
      />
      {list.isError && <ErrorNotice error={list.error} title="Could not read competitions" />}
      {list.isPending ? (
        <Skeleton label="Reading competitions from BRAIN" />
      ) : live.length === 0 ? (
        <Panel>
          <Empty icon={<TrophyIcon />} title="No competition is running right now." />
        </Panel>
      ) : (
        live.map((c) => <CompetitionCard key={c.id} competition={c} />)
      )}
      {past.length > 0 && (
        <Disclosure summary={`Past Competitions (${fmt.int(past.length)})`}>
          <ul className="flex flex-col gap-2 text-body">
            {past.map((c) => (
              <li key={c.id} className="flex flex-wrap items-baseline justify-between gap-x-4">
                <span>{c.name}</span>
                <span className="text-body-compact text-ink-subtle">
                  {fmt.date(c.startDate)} – {fmt.date(c.endDate)}
                  {c.enrolled ? ' · enrolled' : ''}
                </span>
              </li>
            ))}
          </ul>
        </Disclosure>
      )}
    </Page>
  )
}
