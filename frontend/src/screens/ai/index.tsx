/**
 * LLM Integration (CLAUDE.md §4.6), provider first: until a key exists the page is only the
 * provider grid, and only then do the tabs appear (`/ai/$tab`, `/ai/assistant/$threadId`).
 */

import { Link, Navigate, useParams } from '@tanstack/react-router'
import { AI_TABS } from '@/shell/nav'
import {
  Empty,
  ErrorNotice,
  LINK,
  Page,
  PageHeader,
  Panel,
  Skeleton,
  TabBar,
  TabLink,
} from '@/ui/kit'
import { Assistant } from './assistant'
import { Budget } from './budget'
import { Keys } from './keys'
import { Models } from './models'
import { Providers } from './providers'
import { useKeys } from './shared'

const SCREENS = { keys: Keys, models: Models, budget: Budget } as const

export function AiScreen() {
  const params = useParams({ strict: false })
  const keys = useKeys()
  const threadId = params.threadId ? Number(params.threadId) || null : null
  const tab = params.threadId ? 'assistant' : params.tab
  const Screen = tab && tab in SCREENS ? SCREENS[tab as keyof typeof SCREENS] : null
  const hasKeys = (keys.data?.keys.length ?? 0) > 0

  // Every branch keeps its slot, so the provider grid (and its open popup) stays mounted when
  // the first key lands and the tab bar appears above it. Without a key every tab shows that
  // grid, so the URL is moved to it too: otherwise the first key switches `/ai/keys` over to
  // the Keys screen and takes the popup, mid-setup, with it.
  return (
    <Page>
      {keys.isSuccess && !hasKeys && tab !== 'providers' && (
        <Navigate to="/ai/$tab" params={{ tab: 'providers' }} replace />
      )}
      <PageHeader title="LLM Integration" />
      {keys.isError && <ErrorNotice title="Could not load the Keys" error={keys.error} />}
      {hasKeys && (
        <TabBar>
          {AI_TABS.map((t) => (
            <TabLink key={t.tab} to="/ai/$tab" params={{ tab: t.tab }}>
              {t.label}
            </TabLink>
          ))}
        </TabBar>
      )}
      {keys.isPending ? (
        <Skeleton className="h-64" />
      ) : !hasKeys || tab === 'providers' ? (
        <Providers />
      ) : tab === 'assistant' ? (
        <Assistant threadId={threadId} />
      ) : Screen ? (
        <Screen />
      ) : (
        <Panel>
          <Empty title={`There is no “${tab}” tab`}>
            <Link to="/ai/$tab" params={{ tab: 'providers' }} className={LINK}>
              Go to Providers
            </Link>
          </Empty>
        </Panel>
      )}
    </Page>
  )
}
