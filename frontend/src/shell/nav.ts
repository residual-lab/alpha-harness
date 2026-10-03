import {
  ChartPieIcon,
  DatabaseIcon,
  FlaskConicalIcon,
  Grid3x3Icon,
  LayersIcon,
  LayoutGridIcon,
  ListChecksIcon,
  RefreshCwIcon,
  ScrollTextIcon,
  SparklesIcon,
  TriangleIcon,
  TrophyIcon,
  WrenchIcon,
} from 'lucide-react'

/** Sub-tabs, in display order. Screens import these so the sidebar, ⌘K and tab bars agree. */
export const POOL_TABS = [
  { tab: 'stored', label: 'Stored' },
  { tab: 'submittable', label: 'Submittable' },
] as const

export const AI_TABS = [
  { tab: 'providers', label: 'Providers' },
  { tab: 'keys', label: 'Keys' },
  { tab: 'models', label: 'Models' },
  { tab: 'budget', label: 'Budget' },
  { tab: 'assistant', label: 'Assistant' },
] as const

/** The labs, each with its own route. Listed on the Research Labs screen and in ⌘K rather
 *  than under the sidebar entry: four children under one item is most of the sidebar, and
 *  the screen already introduces them properly. */
export const LAB_TABS = [
  { tab: 'search', label: 'Search Lab', to: '/labs/search' },
  { tab: 'template', label: 'Template Lab', to: '/labs/template' },
  { tab: 'evolution', label: 'Evolution Lab', to: '/labs/evolution' },
  { tab: 'power-pool', label: 'LLM Power Pool Lab', to: '/labs/power-pool' },
  { tab: 'region-agnostic', label: 'Region Agnostic Lab', to: '/labs/region-agnostic' },
  { tab: 'super-alpha', label: 'Super Alpha Lab', to: '/labs/super-alpha' },
] as const

/** The tools, each with its own route. Listed on the Tools screen and in ⌘K rather than
 *  under the sidebar entry, for the same reason as the labs. */
export const TOOL_TABS = [
  { tab: 'settings-sampler', label: 'Settings Sampler', to: '/tools/settings-sampler' },
  { tab: 'submission-planner', label: 'Submission Planner', to: '/tools/submission-planner' },
  { tab: 'correlation-breaker', label: 'Correlation Breaker', to: '/tools/correlation-breaker' },
] as const

/** `group` heads a run of items in the sidebar; empty sits above every heading. An item's
 *  area is its path, `to` without the slash. */
export const NAV = [
  {
    to: '/dashboard',
    group: '',
    label: 'Dashboard',
    icon: LayoutGridIcon,
  },
  {
    to: '/matrix',
    group: '',
    label: 'Simulation Matrix',
    icon: Grid3x3Icon,
  },
  {
    to: '/data',
    group: 'Research',
    label: 'Data Explorer',
    icon: DatabaseIcon,
  },
  {
    to: '/labs',
    group: 'Research',
    label: 'Research Labs',
    icon: FlaskConicalIcon,
    tabs: LAB_TABS,
  },
  {
    to: '/tools',
    group: 'Research',
    label: 'Tools',
    icon: WrenchIcon,
    tabs: TOOL_TABS,
  },
  {
    to: '/prompts',
    group: 'Research',
    label: 'LLM Prompts',
    icon: ScrollTextIcon,
  },
  {
    to: '/tasks',
    group: 'Results',
    label: 'Tasks',
    icon: ListChecksIcon,
  },
  {
    to: '/pool',
    group: 'Results',
    label: 'Alphas',
    icon: LayersIcon,
    tabs: POOL_TABS,
  },
  {
    to: '/portfolio',
    group: 'Results',
    label: 'Portfolio',
    icon: ChartPieIcon,
  },
  {
    to: '/competitions',
    group: 'BRAIN',
    label: 'Competitions',
    icon: TrophyIcon,
  },
  {
    to: '/pyramids',
    group: 'BRAIN',
    label: 'Pyramids',
    icon: TriangleIcon,
  },
  {
    to: '/sync',
    group: 'BRAIN',
    label: 'Sync',
    icon: RefreshCwIcon,
  },
  {
    to: '/ai',
    group: 'Setup',
    label: 'LLM Integration',
    icon: SparklesIcon,
    tabs: AI_TABS,
  },
] as const
