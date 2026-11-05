/** Tools: every tool, two by two, in the same cards as Research Labs so the two group
 *  screens read as one pattern. */

import { Link } from '@tanstack/react-router'
import { GitForkIcon, type LucideIcon, SlidersHorizontalIcon, UnlinkIcon } from 'lucide-react'
import { HUB_CARD, HubCardBody } from '@/screens/research-labs'
import { TOOL_TABS } from '@/shell/nav'
import { Page, PageHeader } from '@/ui/kit'

const ICONS: Record<(typeof TOOL_TABS)[number]['tab'], LucideIcon> = {
  'settings-sampler': SlidersHorizontalIcon,
  'correlation-breaker': UnlinkIcon,
  'fields-sampler': GitForkIcon,
}

/** What each one is for, since a name alone does not say when to reach for it. */
const ABOUT: Record<(typeof TOOL_TABS)[number]['tab'], string> = {
  'settings-sampler': 'Sweep one Alpha across Simulation Settings to find where it works best.',
  'correlation-breaker': 'Re-shape an Alpha that is already in the Production Pool.',
  'fields-sampler':
    "Swap one Alpha's data fields for others, one at a time, and grow from every swap that passes.",
}

/** Each tool's own search params. Both take one and default it to nothing, and a `Link`
 *  cannot infer that through the union of routes. */
const SEARCH = {
  'settings-sampler': { alpha: undefined },
  'correlation-breaker': { alpha: undefined },
  'fields-sampler': { alpha: undefined },
} as const

export function ToolsScreen() {
  return (
    <Page>
      <PageHeader title="Tools" />
      <div className="grid gap-3 sm:grid-cols-2">
        {TOOL_TABS.map((tool, index) => (
          <Link key={tool.tab} to={tool.to} search={SEARCH[tool.tab]} className={HUB_CARD}>
            <HubCardBody
              icon={ICONS[tool.tab]}
              index={index}
              label={tool.label}
              about={ABOUT[tool.tab]}
            />
          </Link>
        ))}
      </div>
    </Page>
  )
}
