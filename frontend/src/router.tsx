/**
 * Code-based route tree. Screens are lazy chunks; sub-tabs and ids are typed path params.
 */

import {
  createRootRoute,
  createRoute,
  createRouter,
  type ErrorComponentProps,
  lazyRouteComponent,
  redirect,
} from '@tanstack/react-router'
import { Shell } from '@/shell/shell'
import { Button, Empty, ErrorNotice, Page, Panel } from '@/ui/kit'

const root = createRootRoute({ component: Shell })

const index = createRoute({
  getParentRoute: () => root,
  path: '/',
  beforeLoad: () => {
    throw redirect({ to: '/dashboard' })
  },
})

const dashboard = createRoute({
  getParentRoute: () => root,
  path: '/dashboard',
  component: lazyRouteComponent(() => import('@/screens/dashboard'), 'DashboardScreen'),
})

const matrix = createRoute({
  getParentRoute: () => root,
  path: '/matrix',
  component: lazyRouteComponent(() => import('@/screens/matrix'), 'MatrixScreen'),
})

const data = createRoute({
  getParentRoute: () => root,
  path: '/data',
  component: lazyRouteComponent(() => import('@/screens/data'), 'DataScreen'),
})

const labs = createRoute({ getParentRoute: () => root, path: '/labs' })
const labsIndex = createRoute({
  getParentRoute: () => labs,
  path: '/',
  component: lazyRouteComponent(() => import('@/screens/research-labs'), 'ResearchLabsScreen'),
})
const searchLab = createRoute({
  getParentRoute: () => labs,
  path: 'search',
  component: lazyRouteComponent(() => import('@/screens/research-labs/search'), 'SearchLabScreen'),
})
const templateLab = createRoute({
  getParentRoute: () => labs,
  path: 'template',
  component: lazyRouteComponent(
    () => import('@/screens/research-labs/template'),
    'TemplateLabScreen',
  ),
})
const templateBasicLab = createRoute({
  getParentRoute: () => labs,
  path: 'template/basic',
  component: lazyRouteComponent(
    () => import('@/screens/research-labs/template-basic'),
    'BasicTemplateScreen',
  ),
})
const evolutionLab = createRoute({
  getParentRoute: () => labs,
  path: 'evolution',
  component: lazyRouteComponent(
    () => import('@/screens/research-labs/evolution'),
    'EvolutionLabScreen',
  ),
})
const powerPoolLab = createRoute({
  getParentRoute: () => labs,
  path: 'power-pool',
  component: lazyRouteComponent(
    () => import('@/screens/research-labs/power-pool'),
    'PowerPoolLabScreen',
  ),
})
const regionAgnosticLab = createRoute({
  getParentRoute: () => labs,
  path: 'region-agnostic',
  component: lazyRouteComponent(
    () => import('@/screens/research-labs/region-agnostic'),
    'RegionAgnosticLabScreen',
  ),
})
const superAlphaLab = createRoute({
  getParentRoute: () => labs,
  path: 'super-alpha',
  component: lazyRouteComponent(
    () => import('@/screens/research-labs/super-alpha'),
    'SuperAlphaLabScreen',
  ),
})
const tools = createRoute({ getParentRoute: () => root, path: '/tools' })
const toolsIndex = createRoute({
  getParentRoute: () => tools,
  path: '/',
  component: lazyRouteComponent(() => import('@/screens/tools'), 'ToolsScreen'),
})
const settingsSampler = createRoute({
  getParentRoute: () => tools,
  path: 'settings-sampler',
  // Tools take typed search params: an id belongs in the URL so a link from the Alpha screen
  // is shareable, unlike the multi-item picks that use a handoff store.
  validateSearch: (search: Record<string, unknown>) => ({
    alpha: typeof search['alpha'] === 'string' ? search['alpha'] : undefined,
  }),
  component: lazyRouteComponent(
    () => import('@/screens/tools/settings-sampler'),
    'SettingsSamplerScreen',
  ),
})

const submissionPlanner = createRoute({
  getParentRoute: () => tools,
  path: 'submission-planner',
  validateSearch: (search: Record<string, unknown>) => ({
    task: typeof search['task'] === 'number' ? search['task'] : undefined,
  }),
  component: lazyRouteComponent(
    () => import('@/screens/tools/submission-planner'),
    'SubmissionPlannerScreen',
  ),
})

const correlationBreaker = createRoute({
  getParentRoute: () => tools,
  path: 'correlation-breaker',
  validateSearch: (search: Record<string, unknown>) => ({
    alpha: typeof search['alpha'] === 'string' ? search['alpha'] : undefined,
  }),
  component: lazyRouteComponent(
    () => import('@/screens/tools/correlation-breaker'),
    'CorrelationBreakerScreen',
  ),
})

/** `open` is the prompt on screen: a saved prompt's id, or a built-in's slug. A number stays
 *  one, since the router quotes a numeric string in the URL (`?open=%221%22`). */
const llmPrompts = createRoute({
  getParentRoute: () => root,
  path: '/prompts',
  validateSearch: (search: Record<string, unknown>): { open?: string | number | undefined } => ({
    open:
      typeof search['open'] === 'string' || typeof search['open'] === 'number'
        ? search['open']
        : undefined,
  }),
  component: lazyRouteComponent(() => import('@/screens/prompts'), 'PromptsScreen'),
})

const tasks = createRoute({ getParentRoute: () => root, path: '/tasks' })
const tasksIndex = createRoute({
  getParentRoute: () => tasks,
  path: '/',
  component: lazyRouteComponent(() => import('@/screens/tasks'), 'TasksScreen'),
})
/** One task's whole result set, with room for the statistics the card cannot hold. */
const taskResults = createRoute({
  getParentRoute: () => tasks,
  path: '$taskId',
  component: lazyRouteComponent(() => import('@/screens/tasks/results'), 'TaskResultsScreen'),
})

const pool = createRoute({ getParentRoute: () => root, path: '/pool' })
const poolIndex = createRoute({
  getParentRoute: () => pool,
  path: '/',
  beforeLoad: () => {
    throw redirect({ to: '/pool/$tab', params: { tab: 'stored' } })
  },
})
const poolTab = createRoute({
  getParentRoute: () => pool,
  path: '$tab',
  component: lazyRouteComponent(() => import('@/screens/pool'), 'PoolScreen'),
})

const portfolio = createRoute({
  getParentRoute: () => root,
  path: '/portfolio',
  component: lazyRouteComponent(() => import('@/screens/portfolio'), 'PortfolioScreen'),
})

const alpha = createRoute({
  getParentRoute: () => root,
  path: '/alpha/$alphaId',
  component: lazyRouteComponent(() => import('@/screens/alpha'), 'AlphaScreen'),
})

// The screen belongs to ``/ai`` itself; the two routes below only carry the params it reads.
// Mounting it on each of them instead remounts the screen when a reply moves the URL to its
// new thread, losing what that reply just said.
const ai = createRoute({
  getParentRoute: () => root,
  path: '/ai',
  component: lazyRouteComponent(() => import('@/screens/ai'), 'AiScreen'),
})
const aiIndex = createRoute({
  getParentRoute: () => ai,
  path: '/',
  beforeLoad: () => {
    throw redirect({ to: '/ai/$tab', params: { tab: 'providers' } })
  },
})
const aiTab = createRoute({
  getParentRoute: () => ai,
  path: '$tab',
  // Prompts moved to their own screen, where they can be edited; old links follow them.
  beforeLoad: ({ params }) => {
    if (params.tab === 'prompts') throw redirect({ to: '/prompts', search: {} })
  },
})
const aiThread = createRoute({ getParentRoute: () => ai, path: 'assistant/$threadId' })

const competitionsRoute = createRoute({
  getParentRoute: () => root,
  path: '/competitions',
  component: lazyRouteComponent(() => import('@/screens/competitions'), 'CompetitionsScreen'),
})

const sync = createRoute({
  getParentRoute: () => root,
  path: '/sync',
  component: lazyRouteComponent(() => import('@/screens/sync'), 'SyncScreen'),
})

const pyramids = createRoute({
  getParentRoute: () => root,
  path: '/pyramids',
  component: lazyRouteComponent(() => import('@/screens/pyramids'), 'PyramidsScreen'),
})

const routeTree = root.addChildren([
  index,
  dashboard,
  matrix,
  data,
  labs.addChildren([
    labsIndex,
    searchLab,
    templateLab,
    templateBasicLab,
    evolutionLab,
    powerPoolLab,
    regionAgnosticLab,
    superAlphaLab,
  ]),
  tools.addChildren([toolsIndex, settingsSampler, submissionPlanner, correlationBreaker]),
  llmPrompts,
  competitionsRoute,
  tasks.addChildren([tasksIndex, taskResults]),
  pool.addChildren([poolIndex, poolTab]),
  portfolio,
  alpha,
  ai.addChildren([aiIndex, aiTab, aiThread]),
  pyramids,
  sync,
])

/** A screen that throws says so instead of going blank (CLAUDE.md anti-goal 3), and offers
 * the way back: a render that failed on a half-loaded query succeeds on a second try. */
function RouteError({ error, reset }: ErrorComponentProps) {
  return (
    <Page>
      <ErrorNotice error={error} title="This screen failed to render" />
      <Button onClick={reset}>Try again</Button>
    </Page>
  )
}

function NotFound() {
  return (
    <Page>
      <Panel>
        <Empty title="There is no screen here">
          Use the sidebar, or press ⌘K to go to a screen or lab.
        </Empty>
      </Panel>
    </Page>
  )
}

export const router = createRouter({
  routeTree,
  defaultPreload: 'intent',
  defaultErrorComponent: RouteError,
  defaultNotFoundComponent: NotFound,
})

declare module '@tanstack/react-router' {
  interface Register {
    router: typeof router
  }
}
