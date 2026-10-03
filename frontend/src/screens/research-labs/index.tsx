/** Research Labs: every lab, two by two. */

import { Link } from '@tanstack/react-router'
import {
  ArrowUpRightIcon,
  BracesIcon,
  DnaIcon,
  GlobeIcon,
  LayersIcon,
  type LucideIcon,
  SearchIcon,
  ZapIcon,
} from 'lucide-react'
import { LAB_TABS } from '@/shell/nav'
import { Page, PageHeader } from '@/ui/kit'

const ICONS: Record<(typeof LAB_TABS)[number]['tab'], LucideIcon> = {
  search: SearchIcon,
  template: BracesIcon,
  evolution: DnaIcon,
  'power-pool': ZapIcon,
  'region-agnostic': GlobeIcon,
  'super-alpha': LayersIcon,
}

/** What each one searches, since a name alone does not say which to start with. */
const ABOUT: Record<(typeof LAB_TABS)[number]['tab'], string> = {
  search: 'Writes one- and two-operator Alphas from the datasets you pick, steering to Sharpe.',
  template:
    'Searches a template you type with $variables for fields, values and operators, or one you build from blocks.',
  evolution: 'Breeds new Alphas from seeds you pick, holding the last two years back as a test.',
  'power-pool': 'An LLM writes Power Pool Alphas for the datasets you pick.',
  'region-agnostic':
    'An LLM writes Alphas from region-agnostic fields, each run in every region that carries them.',
  'super-alpha': "Combines your submitted Alphas into SuperAlphas that pass BRAIN's checks.",
}

/** The card a hub screen links each of its entries with; the `Link` carries it. */
export const HUB_CARD =
  'panel-highlight group flex min-h-56 flex-col justify-between rounded-lg border border-hairline bg-surface-1 p-6 transition-colors hover:border-hairline-strong hover:bg-surface-2'

export function HubCardBody({
  icon: Icon,
  index,
  label,
  about,
}: {
  icon: LucideIcon
  index: number
  label: string
  about?: string
}) {
  return (
    <>
      <div className="flex items-start justify-between">
        <span className="flex size-12 items-center justify-center rounded-md border border-hairline-strong bg-surface-2 text-ink-muted transition-colors group-hover:border-primary group-hover:text-primary">
          <Icon className="size-6" aria-hidden />
        </span>
        <ArrowUpRightIcon
          className="size-5 text-ink-tertiary transition-colors group-hover:text-ink"
          aria-hidden
        />
      </div>
      <div className="flex flex-col gap-1">
        <span className="num text-body-compact text-ink-subtle">
          {String(index + 1).padStart(2, '0')}
        </span>
        <h2 className="text-headline font-semibold text-balance text-ink">{label}</h2>
        {about && <p className="max-w-prose text-body text-pretty text-ink-subtle">{about}</p>}
      </div>
    </>
  )
}

export function ResearchLabsScreen() {
  return (
    <Page>
      <PageHeader title="Research Labs" />
      <div className="grid gap-3 sm:grid-cols-2">
        {LAB_TABS.map((lab, index) => (
          <Link key={lab.tab} to={lab.to} className={HUB_CARD}>
            <HubCardBody
              icon={ICONS[lab.tab]}
              index={index}
              label={lab.label}
              about={ABOUT[lab.tab]}
            />
          </Link>
        ))}
      </div>
    </Page>
  )
}
