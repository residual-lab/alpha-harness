/**
 * Settings Sampler: one proven expression, re-run everywhere BRAIN will accept it.
 *
 * One source of truth: the set of chosen markets. The Region, Delay and Universe rows are
 * views of that set rather than filters beside it, so a chip is full, part-full or empty
 * according to what is actually chosen, and clicking it selects or clears its whole group.
 * That keeps the chips and the tree from ever disagreeing while still letting a single
 * market — EUR delay 1, say — be dropped on its own.
 */

import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { useNavigate, useSearch } from '@tanstack/react-router'
import { PencilIcon } from 'lucide-react'
import { type ReactNode, useEffect, useMemo, useState } from 'react'
import { today } from '@/api/core'
import {
  DEFAULT_TRUNCATION,
  MULTI_SIMULATION_SIZE,
  RA_CHILDREN,
  REGION_AGNOSTIC_REGION,
  TEST_PERIOD_YEARS,
} from '@/api/platform'
import { cn } from '@/lib/cn'
import { DASH, fmt } from '@/lib/format'
import { neutralizationLabel, splitNeutralizations } from '@/lib/neutralization'
import { useCores } from '@/lib/preferences'
import { DEFAULT_SCOPE, marketKey, regionLabel, useScopeOptions } from '@/lib/scope'
import { useDebounced } from '@/lib/use-debounced'
import { alphaIdOf } from '@/screens/pool/api'
import { AstInspector } from '@/screens/pool/shared'
import { AddTaskButtons, useAddTask } from '@/screens/research-labs/add-task'
import {
  SimulationSettingsFields,
  testPeriodOf,
  truncationOf,
} from '@/screens/research-labs/simulation-settings'
import { templateLab } from '@/screens/research-labs/template/api'
import { TemplateEditor } from '@/screens/research-labs/template/editor'
import { Facts } from '@/screens/research-labs/template/gallery'
import {
  Button,
  Empty,
  ErrorNotice,
  Field,
  Fieldset,
  Input,
  Metric,
  Notice,
  Page,
  PageHeader,
  Panel,
  Segmented,
  Skeleton,
  Switch,
} from '@/ui/kit'
import { Tile, ToggleTitle } from '@/ui/tile'
import {
  type Holding,
  type MarketPick,
  type Pair,
  pairLabel,
  type SettingsPlan,
  type Source,
  settingsSampler,
} from './api'

/** A multi-simulation carries at most ten children, all sharing region and delay. BRAIN fails
 *  a batch of region-agnostic ones, so those go one at a time. */
const batchOf = (region: string) => (region === REGION_AGNOSTIC_REGION ? 1 : MULTI_SIMULATION_SIZE)

/** A plain expression has no template variables; held once so the editor never relints for them. */
const NO_PRESETS: Record<string, string> = {}
const NO_VARIABLES = {}
const NO_INFOS = new Map()
const NO_PROBLEMS: string[] = []

const REGION_AGNOSTIC_NOTE =
  `Region Agnostic: each Simulation runs in every Region its Fields reach and uses ${RA_CHILDREN} of the ` +
  "day's simulations. Not chosen until you tick it."

const pairKey = (p: Pair) => `${p.maxTrade}|${p.maxPosition}`

type Mode = 'expression' | 'alpha'

const MODES: { value: Mode; label: string }[] = [
  { value: 'expression', label: 'Expression' },
  { value: 'alpha', label: 'Alpha ID' },
]
/** TOP200 before TOP1000: universe names are numbered, so compare them that way. */
const byName = (a: string, b: string) => a.localeCompare(b, undefined, { numeric: true })

/**
 * Each family's neutralizations, three to a row. Risk: the fundamental factor models with
 * their combination last, then the other risk models. Other: no grouping and the geographic
 * groups, then the GICS hierarchy from sector down.
 */
const NEUTRALIZATION_ORDER = [
  'FAST',
  'SLOW',
  'SLOW_AND_FAST',
  'REVERSION_AND_MOMENTUM',
  'CROWDING',
  'STATISTICAL',
  'NONE',
  'MARKET',
  'COUNTRY',
  'SECTOR',
  'INDUSTRY',
  'SUBINDUSTRY',
]

/** Universes without a stock count in their name: the volume floors, then ALL's size buckets. */
const UNCOUNTED_UNIVERSES = ['MINVOL1M', 'MINVOL10M', 'LARGE', 'MEDIUM', 'SMALL']

/**
 * Smallest universe first. A named index sits just after the TOP universe of its size
 * (TOPSP500 after TOP500, TOP2000U after TOP2000), and the uncounted ones close the list.
 */
const bySize = (a: string, b: string) => {
  const key = (name: string): [number, number] => {
    const count = /^TOP\D*(\d+)/.exec(name)?.[1]
    if (count) return [Number(count), name.length]
    const at = UNCOUNTED_UNIVERSES.indexOf(name)
    return [Number.POSITIVE_INFINITY, at === -1 ? UNCOUNTED_UNIVERSES.length : at]
  }
  const [x, y] = [key(a), key(b)]
  return x[0] - y[0] || x[1] - y[1] || byName(a, b)
}

const allMarkets = (plan: SettingsPlan) => plan.regions.flatMap((r) => r.markets)

/** Every market but All Regions, which costs four a simulation and so is only swept on
 *  purpose, every neutralization and every pair. */
function defaults(plan: SettingsPlan) {
  return {
    chosen: new Set(
      allMarkets(plan)
        .filter((m) => m.region !== REGION_AGNOSTIC_REGION)
        .map(marketKey),
    ),
    neutralizations: [...new Set(plan.regions.flatMap((r) => r.neutralizations))],
    pairs: [...new Set(plan.regions.flatMap((r) => r.pairs.map(pairKey)))],
  }
}

/** How much of a group is chosen. Drives both the look of a chip and what clicking it does. */
interface Group {
  keys: string[]
  on: number
}

const groupOf = (
  markets: { region: string; delay: number; universe: string }[],
  chosen: ReadonlySet<string>,
): Group => {
  const keys = markets.map(marketKey)
  return { keys, on: keys.filter((k) => chosen.has(k)).length }
}

/** One region once the choice is applied: what survives, and what it costs. */
interface Branch {
  region: string
  group: Group
  neutralizations: number
  /** The constraints swept here, `MP / MT`, or `None` when it runs unconstrained only. */
  investability: string
  rows: {
    delay: number
    group: Group
    universes: { name: string; key: string; on: boolean }[]
  }[]
  markets: number
  total: number
}

function resolve(
  plan: SettingsPlan | undefined,
  chosen: ReadonlySet<string>,
  neutralizations: string[],
  pairs: string[],
  marketNeutralOnly: boolean,
) {
  const neutral = new Set(neutralizations)
  const pairSet = new Set(pairs)
  const branches: Branch[] = []
  const picks: MarketPick[] = []
  const perDelay = new Map<string, number>()

  for (const region of plan?.regions ?? []) {
    const live = region.markets
    const neutHere = region.neutralizations.filter((n) => neutral.has(n)).length
    const legalPairs = region.pairs.filter((p) => pairSet.has(pairKey(p)))
    const pairsHere = legalPairs.length
    // The one combination the sweep drops when it is asked to: neither neutralized nor
    // constrained. Counted per market, so the estimate matches what actually runs.
    const unhedged =
      marketNeutralOnly &&
      neutral.has('NONE') &&
      region.neutralizations.includes('NONE') &&
      legalPairs.some((p) => p.maxTrade === 'OFF' && p.maxPosition === 'OFF')
        ? 1
        : 0
    const runs = neutHere * pairsHere - unhedged

    const byDelay = new Map<number, typeof live>()
    for (const market of live) {
      byDelay.set(market.delay, [...(byDelay.get(market.delay) ?? []), market])
    }

    let markets = 0
    for (const market of live) {
      if (!chosen.has(marketKey(market))) continue
      markets += 1
      picks.push({ region: market.region, delay: market.delay, universe: market.universe })
      const key = `${market.region}|${market.delay}`
      perDelay.set(key, (perDelay.get(key) ?? 0) + runs)
    }

    branches.push({
      region: region.region,
      group: groupOf(live, chosen),
      neutralizations: neutHere,
      investability:
        [
          legalPairs.some((p) => p.maxPosition === 'ON') && 'MP',
          legalPairs.some((p) => p.maxTrade === 'ON') && 'MT',
        ]
          .filter(Boolean)
          .join(' / ') || 'None',
      rows: [...byDelay.entries()]
        .sort((a, b) => a[0] - b[0])
        .map(([delay, list]) => ({
          delay,
          group: groupOf(list, chosen),
          universes: list
            .map((m) => ({ name: m.universe, key: marketKey(m), on: chosen.has(marketKey(m)) }))
            .sort((a, b) => bySize(a.name, b.name)),
        })),
      markets,
      total: markets * runs * region.cost,
    })
  }

  let batches = 0
  for (const [key, count] of perDelay)
    batches += Math.ceil(count / batchOf(key.slice(0, key.indexOf('|'))))
  return {
    branches,
    picks,
    simulations: branches.reduce((sum, b) => sum + b.total, 0),
    batches,
    markets: picks.length,
  }
}

/** Region, then its three factors and what they come to, kept together at the far right.
 *  The units are named once in the header above the list. */
const GRID =
  'grid min-w-184 grid-cols-[10rem_minmax(0,1fr)_5rem_8rem_7rem_7rem] items-center gap-x-3'

/** Keeps the Region column in view when the list scrolls sideways. A surface, not a shadow:
 *  shadows belong to what floats over the page. */
const STICKY = 'sticky left-0 z-10 bg-surface-1'

/**
 * A chip standing for a group of markets: full, part-full, or empty. Clicking clears the
 * group when any of it is chosen and restores it when none is.
 */
function GroupChip({
  label,
  group,
  onChange,
  size = 'md',
  disabled,
  title,
  className,
}: {
  label: ReactNode
  group: Group
  onChange: (keys: string[], on: boolean) => void
  size?: 'md' | 'sm'
  disabled?: boolean
  title?: string | undefined
  className?: string
}) {
  const { keys, on } = group
  const part = on > 0 && on < keys.length
  return (
    <button
      type="button"
      disabled={disabled || keys.length === 0}
      aria-pressed={on > 0}
      title={part ? `${on} of ${keys.length} chosen` : title}
      onClick={() => onChange(keys, on === 0)}
      className={cn(
        'num flex items-center gap-1.5 rounded-sm border whitespace-nowrap transition-colors',
        size === 'md' ? 'h-8 px-2.5 text-body' : 'h-7 px-2 text-body-compact',
        on === 0 && 'border-(--field-border) bg-surface-1 text-ink-subtle',
        part && 'border-primary/50 bg-surface-2 text-ink-muted',
        on === keys.length && on > 0 && 'border-primary bg-primary-subtle text-ink',
        !disabled && 'hover:border-(--field-border-hover) hover:text-ink',
        className,
      )}
    >
      {label}
      {part && (
        <span className="text-body-compact text-ink-subtle">
          {on}/{keys.length}
        </span>
      )}
    </button>
  )
}

/** One setting in a box of its own: its name and shortcuts on top, its choices below. */
/** Choices sharing the full width evenly, `columns` to a line; scrolls rather than squeezes. */
function Spread({
  columns,
  min = '4rem',
  children,
}: {
  columns: number
  min?: string
  children: ReactNode
}) {
  return (
    <div className="overflow-x-auto">
      <div
        className="grid gap-1.5"
        style={{ gridTemplateColumns: `repeat(${columns}, minmax(${min}, 1fr))` }}
      >
        {children}
      </div>
    </div>
  )
}

function Tree({
  branches,
  onChange,
}: {
  branches: Branch[]
  onChange: (keys: string[], on: boolean) => void
}) {
  return (
    <div className="overflow-x-auto">
      <div
        aria-hidden
        className={cn(GRID, 'px-3 pb-1.5 text-caption tracking-wide text-ink-subtle uppercase')}
      >
        <span className={STICKY}>Region</span>
        <span />
        <span className="text-right">Markets</span>
        <span className="text-right">Neutralizations</span>
        <span className="text-right">Investability</span>
        <span className="text-right">Simulations</span>
      </div>
      <ul className="flex flex-col gap-1.5">
        {branches.map((branch) => (
          <li
            key={branch.region}
            className={cn(
              'rounded-md border border-hairline px-3 py-2 transition-opacity',
              branch.group.on === 0 && 'opacity-55',
            )}
          >
            <div className={GRID}>
              <div className={STICKY}>
                <GroupChip
                  label={<span className="font-medium">{regionLabel(branch.region)}</span>}
                  group={branch.group}
                  onChange={onChange}
                  title={
                    branch.region === REGION_AGNOSTIC_REGION ? REGION_AGNOSTIC_NOTE : undefined
                  }
                />
              </div>
              <span />
              <span className="num text-right text-body-compact text-ink-muted">
                {branch.markets}
              </span>
              <span className="num text-right text-body-compact text-ink-muted">
                {branch.neutralizations}
              </span>
              <span className="num text-right text-body-compact text-ink-muted">
                {branch.investability}
              </span>
              <span className="num text-right text-body font-semibold text-ink">
                {fmt.int(branch.total)}
              </span>
            </div>

            <div className="mt-1.5 flex flex-col gap-1 border-t border-hairline-subtle pt-1.5">
              {branch.rows.map((row) => (
                <div key={row.delay} className="flex flex-wrap items-center gap-1.5">
                  {/* This region's markets at this delay only, so EUR D1 can go on its own. */}
                  <GroupChip
                    size="sm"
                    label={`D${row.delay}`}
                    group={row.group}
                    onChange={onChange}
                  />
                  {row.universes.map((universe) => (
                    <GroupChip
                      key={universe.key}
                      size="sm"
                      label={universe.name}
                      group={{ keys: [universe.key], on: universe.on ? 1 : 0 }}
                      onChange={onChange}
                    />
                  ))}
                </div>
              ))}
            </div>
          </li>
        ))}
      </ul>
    </div>
  )
}

export function SettingsSamplerScreen() {
  const search = useSearch({ from: '/tools/settings-sampler' })
  const navigate = useNavigate()
  // The URL owns which Alpha is open, so arriving without one shows an empty screen rather
  // than the last one analysed.
  const alphaId = search.alpha ?? ''
  const [mode, setMode] = useState<Mode>(alphaId ? 'alpha' : 'expression')
  const [draft, setDraft] = useState(alphaId)
  const [expression, setExpression] = useState('')
  const operators = useQuery({
    queryKey: ['template-lab', 'options'],
    queryFn: () => templateLab.options(),
    staleTime: 5 * 60_000,
  })
  const reference = useMemo(() => operators.data?.reference ?? [], [operators.data])
  // Counted as it is typed, so Power Pool's limits and the dataset count are never a guess.
  const sizing = useDebounced(expression.trim(), 300)
  const sized = useQuery({
    queryKey: ['template-lab', 'stats', 'USA', 1, sizing],
    queryFn: () =>
      templateLab.stats({
        region: 'USA',
        delay: 1,
        templates: [{ text: sizing, variables: {} }],
      }),
    enabled: sizing !== '',
    placeholderData: keepPreviousData,
  })
  const [decay, setDecay] = useState('0')
  const [truncation, setTruncation] = useState(String(DEFAULT_TRUNCATION))
  const [pasteurization, setPasteurization] = useState<'ON' | 'OFF'>('ON')
  const [nanHandling, setNanHandling] = useState<'ON' | 'OFF'>('ON')
  const [testYears, setTestYears] = useState(String(TEST_PERIOD_YEARS))
  const [testMonths, setTestMonths] = useState('0')
  /** The expression last analysed; the draft above only counts once Analyse is pressed. */
  const [typed, setTyped] = useState<Source | null>(null)
  const [chosen, setChosen] = useState<ReadonlySet<string>>(new Set())
  const [neutralizations, setNeutralizations] = useState<string[]>([])
  const [pairs, setPairs] = useState<string[]>([])
  /** `null` until chosen here: until then Settings' default for new tasks applies. */
  const [chosenCores, setCores] = useState<number | null>(null)
  const wantedCores = useCores(chosenCores)
  const [marketNeutralOnly, setMarketNeutralOnly] = useState(true)
  const [truncationAgent, setTruncationAgent] = useState(false)

  useEffect(() => {
    setDraft(search.alpha ?? '')
    if (search.alpha) setMode('alpha')
  }, [search.alpha])

  const holding: Holding = {
    decay: Math.max(0, Math.round(Number(decay) || 0)),
    truncation: truncationOf(truncation) ?? DEFAULT_TRUNCATION,
    pasteurization,
    nanHandling,
    testPeriod: testPeriodOf(testYears, testMonths),
  }
  const source: Source | null = mode === 'alpha' ? (alphaId ? { alphaId } : null) : typed
  const query = useQuery({
    queryKey: ['settings-sampler', source],
    queryFn: () => settingsSampler.preview(source ?? { alphaId: '' }),
    enabled: source !== null,
    retry: false,
  })
  const plan = query.data
  const cores = Math.min(wantedCores, plan?.maxCores ?? wantedCores)

  // An Alpha's expression and settings are copied into the Expression tab to adjust. Until the
  // expression itself is edited and analysed, the sweep still runs the Alpha: the task stays
  // named for it, and the address keeps it, so a reload opens it again.
  useEffect(() => {
    if (!plan?.alphaId || !plan.expression) return
    const own = plan.settings
    setExpression(plan.expression)
    setDecay(String(own.decay ?? 0))
    setTruncation(String(own.truncation ?? DEFAULT_TRUNCATION))
    setPasteurization(own.pasteurization === 'OFF' ? 'OFF' : 'ON')
    setNanHandling(own.nanHandling === 'OFF' ? 'OFF' : 'ON')
    const period = /^P(\d+)Y(\d+)M/.exec(own.testPeriod ?? '')
    setTestYears(period?.[1] ?? String(TEST_PERIOD_YEARS))
    setTestMonths(period?.[2] ?? '0')
    setTyped({ alphaId: plan.alphaId })
  }, [plan])

  const reset = (from: SettingsPlan) => {
    const start = defaults(from)
    setChosen(start.chosen)
    setNeutralizations(start.neutralizations)
    setPairs(start.pairs)
  }

  useEffect(() => {
    if (!plan) return
    const start = defaults(plan)
    setChosen(start.chosen)
    setNeutralizations(start.neutralizations)
    setPairs(start.pairs)
    setCores(null)
  }, [plan])

  const change = (keys: string[], on: boolean) =>
    setChosen((prev) => {
      const next = new Set(prev)
      for (const key of keys) {
        if (on) next.add(key)
        else next.delete(key)
      }
      return next
    })

  const all = useMemo(() => (plan ? allMarkets(plan) : []), [plan])
  const whole = useMemo(
    () =>
      plan
        ? resolve(
            plan,
            new Set(all.map(marketKey)),
            [...new Set(plan.regions.flatMap((r) => r.neutralizations))],
            [...new Set(plan.regions.flatMap((r) => r.pairs.map(pairKey)))],
            marketNeutralOnly,
          ).simulations
        : 0,
    [plan, all, marketNeutralOnly],
  )
  // What the agent sets across the markets chosen, from the plan's own figure for each.
  const agentValues = all.filter((m) => chosen.has(marketKey(m))).map((m) => m.agentTruncation)
  const lowest = Math.min(...agentValues)
  const highest = Math.max(...agentValues)
  const agentSummary =
    agentValues.length === 0 ? (
      'Choose a market to see what it sets.'
    ) : (
      <span title="0.08 in broad universes, 0.06 in mid-sized ones, 0.05 in narrow ones, multi-country regions and Delay 0. Never above 0.08, under the 8% Weight Test.">
        Set per Market:{' '}
        <span className="num text-ink">
          {lowest === highest
            ? fmt.ratio(lowest, 2)
            : `${fmt.ratio(lowest, 2)} to ${fmt.ratio(highest, 2)}`}
        </span>{' '}
        across the chosen markets.
      </span>
    )
  const { branches, picks, simulations, batches, markets } = useMemo(
    () => resolve(plan, chosen, neutralizations, pairs, marketNeutralOnly),
    [plan, chosen, neutralizations, pairs, marketNeutralOnly],
  )
  // What the skip is worth right now, so the choice is made against a number.
  const unhedged = useMemo(
    () =>
      resolve(plan, chosen, neutralizations, pairs, false).simulations -
      resolve(plan, chosen, neutralizations, pairs, true).simulations,
    [plan, chosen, neutralizations, pairs],
  )

  /** The Region, Delay and Universe rows, each a view of the chosen markets. */
  const axes = useMemo(() => {
    const by = <K extends string | number>(pick: (m: (typeof all)[number]) => K) => {
      const buckets = new Map<K, typeof all>()
      for (const market of all)
        buckets.set(pick(market), [...(buckets.get(pick(market)) ?? []), market])
      return [...buckets.entries()]
    }
    return {
      // Most universes first, so the broadest markets lead; ties alphabetical.
      regions: by((m) => m.region)
        .map(([value, list]) => ({
          value,
          universes: new Set(list.map((m) => m.universe)).size,
          group: groupOf(list, chosen),
        }))
        .sort((a, b) => b.universes - a.universes || a.value.localeCompare(b.value)),
      delays: by((m) => m.delay)
        .sort((a, b) => a[0] - b[0])
        .map(([value, list]) => ({ value, group: groupOf(list, chosen) })),
      universes: by((m) => m.universe)
        .sort((a, b) => bySize(a[0], b[0]))
        .map(([value, list]) => ({ value, group: groupOf(list, chosen) })),
    }
  }, [all, chosen])

  const allPairs = useMemo(
    () =>
      (plan?.regions ?? [])
        .flatMap((r) => r.pairs)
        .filter((p, i, list) => list.findIndex((q) => pairKey(q) === pairKey(p)) === i),
    [plan],
  )
  // Every neutralization the sweep's markets offer between them, under BRAIN's own labels
  // where the source market knows them. A sweep spans regions, so one that only exists
  // elsewhere keeps its bare name rather than being dropped.
  const labelled = useScopeOptions({
    instrumentType: 'EQUITY',
    region: String(plan?.settings.region ?? DEFAULT_SCOPE.region),
    delay: Number(plan?.settings.delay ?? DEFAULT_SCOPE.delay),
    universe: String(plan?.settings.universe ?? DEFAULT_SCOPE.universe),
  }).neutralizations
  const allNeutralizations = useMemo(() => {
    const names = [...new Set((plan?.regions ?? []).flatMap((r) => r.neutralizations))]
    const labels = new Map(labelled.map((c) => [c.value, c.label]))
    // Laid out three to a row, each row one family, coarse to fine. One BRAIN adds later
    // follows in BRAIN's own order.
    const rank = (v: string) => {
      const at = NEUTRALIZATION_ORDER.indexOf(v)
      if (at !== -1) return at
      const brain = labelled.findIndex((c) => c.value === v)
      return NEUTRALIZATION_ORDER.length + (brain === -1 ? labelled.length : brain)
    }
    return names
      .sort((a, b) => rank(a) - rank(b) || a.localeCompare(b))
      .map((value) => ({ value, label: neutralizationLabel(value, labels.get(value)) }))
  }, [plan, labelled])

  const anyMarketOn = markets > 0
  const anyNeutralizationOn = neutralizations.length > 0
  const toggleMarkets = () => change(all.map(marketKey), !anyMarketOn)

  // The header's own query, so both read one cache. What is already queued will spend first.
  const bar = useQuery({ queryKey: ['bar'], queryFn: () => today.bar() })
  const quota = bar.data?.simulations
  const left = quota ? Math.max(0, quota.remaining - quota.queued) : null
  const overQuota = left !== null && simulations > left
  const leftLabel = quota
    ? `${quota.exact ? '' : '~'}${fmt.int(left)} left today${quota.queued ? ' after queued work' : ''}`
    : undefined

  const add = useAddTask(() =>
    settingsSampler.addTask({
      ...(source ?? { alphaId: '' }),
      ...holding,
      markets: picks,
      marketNeutralOnly,
      truncationAgent,
      neutralizations,
      pairs: allPairs.filter((p) => pairs.includes(pairKey(p))),
      cores,
    }),
  )

  const analyse = (event: React.FormEvent) => {
    event.preventDefault()
    if (mode === 'expression') {
      const text = expression.trim()
      // Analysed unchanged, it is still the Alpha; changed, it is an expression of its own.
      if (typed && 'alphaId' in typed && text === plan?.expression) return
      setTyped({ expression: text })
      if (alphaId)
        void navigate({
          to: '/tools/settings-sampler',
          search: { alpha: undefined },
          replace: true,
        })
      return
    }
    const next = draft.trim()
    void navigate({
      to: '/tools/settings-sampler',
      search: { alpha: next || undefined },
      replace: true,
    })
  }

  // The sweep runs what was last analysed, so an edit since then must be analysed first.
  const analysed = typed && ('expression' in typed ? typed.expression : plan?.expression)
  const edited = mode === 'expression' && analysed != null && analysed !== expression.trim()
  // Said on the Truncation box itself.
  const badTruncation = !truncationAgent && truncationOf(truncation) === null

  return (
    <Page>
      <PageHeader
        sticky
        title="Settings Sampler"
        description="Run an expression everywhere BRAIN accepts it"
        actions={
          <AddTaskButtons
            add={add}
            disabled={simulations === 0 || edited || badTruncation}
            blocked={edited ? 'Edited since the last Analyse: Analyse again.' : null}
          />
        }
      />

      <Panel
        title="Source"
        actions={<Segmented label="Source" items={MODES} value={mode} onChange={setMode} />}
      >
        {mode === 'alpha' ? (
          <form onSubmit={analyse} className="flex flex-wrap items-end gap-3">
            <Field label="Alpha ID" className="min-w-48 flex-1">
              <Input
                value={draft}
                onChange={(e) => setDraft(alphaIdOf(e.target.value))}
                spellCheck={false}
                className="num"
              />
            </Field>
            <Button type="submit" variant="primary" disabled={!draft.trim()}>
              Analyse
            </Button>
            {plan?.alphaId && plan.expression && (
              <div className="flex w-full flex-col gap-2">
                <AstInspector expression={plan.expression} />
                <Button
                  type="button"
                  size="sm"
                  className="self-start"
                  onClick={() => setMode('expression')}
                >
                  <PencilIcon />
                  Edit Expression
                </Button>
              </div>
            )}
          </form>
        ) : (
          <form onSubmit={analyse} className="flex flex-col gap-3">
            <Fieldset legend="Alpha Expression">
              <TemplateEditor
                label="Alpha Expression"
                value={expression}
                onChange={setExpression}
                reference={reference}
                presets={NO_PRESETS}
                variables={NO_VARIABLES}
                infos={NO_INFOS}
                problems={NO_PROBLEMS}
              />
            </Fieldset>
            <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
              <Button type="submit" variant="primary" disabled={!expression.trim()}>
                Analyse
              </Button>
              {expression.trim() && <Facts stats={sized.data?.stats[0]} />}
              {edited && (
                <p className="text-body-compact text-ink-subtle">
                  Edited since the last Analyse. Analyse again to sweep this expression.
                </p>
              )}
            </div>
          </form>
        )}

        {plan?.expression && (
          <div className="mt-4 flex flex-col gap-4 border-t border-hairline pt-4">
            <section className="flex flex-col gap-2">
              <h3 className="text-body-compact font-medium tracking-wide text-ink-muted uppercase">
                Data Fields
              </h3>
              <div className="flex flex-wrap gap-2">
                {plan.dataFields.length + plan.groupingFields.length === 0 ? (
                  <span className="text-body-compact text-ink-subtle">{DASH}</span>
                ) : (
                  <>
                    {plan.dataFields.map((field) => (
                      <span
                        key={field}
                        className="num rounded-md border border-hairline-strong bg-surface-2 px-2.5 py-1.5 text-body-compact text-ink"
                      >
                        {field}
                      </span>
                    ))}
                    {/* Read and required in every market, though BRAIN counts none as data. */}
                    {plan.groupingFields.map((field) => (
                      <span
                        key={field}
                        title="Grouping field: must exist where it runs, but BRAIN does not count it as a data field"
                        className="flex items-baseline gap-1.5 rounded-md border border-hairline bg-surface-2 px-2.5 py-1.5 text-body-compact"
                      >
                        <span className="num text-ink">{field}</span>
                        <span className="text-ink-subtle">grouping</span>
                      </span>
                    ))}
                  </>
                )}
              </div>
            </section>
          </div>
        )}
      </Panel>

      {plan?.expression && (
        <Panel
          title="Simulation Settings"
          description={`Every market in the sweep runs at these${truncationAgent ? ', with Truncation set per market' : ''}.`}
        >
          <SimulationSettingsFields
            decay={decay}
            setDecay={setDecay}
            truncation={truncation}
            setTruncation={setTruncation}
            truncationAgent={truncationAgent}
            setTruncationAgent={setTruncationAgent}
            agentSummary={agentSummary}
            pasteurization={pasteurization}
            setPasteurization={setPasteurization}
            nanHandling={nanHandling}
            setNanHandling={setNanHandling}
            testYears={testYears}
            setTestYears={setTestYears}
            testMonths={testMonths}
            setTestMonths={setTestMonths}
          />
        </Panel>
      )}

      {query.isError && <ErrorNotice error={query.error} title="Could not read that Alpha" />}
      {plan?.problems.map((problem) => (
        <Notice key={problem} tone="error" title="Cannot run this Alpha elsewhere">
          {problem}
        </Notice>
      ))}
      {plan?.warnings.map((warning) => (
        <Notice key={warning} tone="warn">
          {warning}
        </Notice>
      ))}

      {query.isPending && source ? (
        <Skeleton className="h-96" />
      ) : plan && branches.length > 0 ? (
        <Panel
          title="Search Space"
          actions={
            <Fieldset legend="Cores">
              <Segmented
                label="Cores"
                items={Array.from({ length: plan.maxCores }, (_, i) => ({
                  value: i + 1,
                  label: i + 1,
                }))}
                value={cores}
                onChange={setCores}
              />
            </Fieldset>
          }
        >
          <div className="flex flex-col gap-5">
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
              <Metric
                boxed
                label="Simulations"
                value={
                  <span>
                    {fmt.int(simulations)}
                    <span className="text-ink-subtle"> / {fmt.int(whole)}</span>
                  </span>
                }
                tone={overQuota ? 'warn' : 'neutral'}
              />
              <Metric boxed label="Markets" value={fmt.int(markets)} />
              <Metric
                boxed
                label="Batches"
                value={fmt.int(batches)}
                // Batches are fixed by the work; cores decide how many run at once.
                hint={
                  batches
                    ? `${cores} at a time \u2248 ${fmt.int(Math.ceil(batches / cores))} rounds`
                    : ''
                }
              />
            </div>

            {overQuota && (
              <Notice tone="warn" title="This sweep needs more than today has left">
                {fmt.int(simulations)} simulations against {leftLabel}. The rest waits in the queue
                and starts once BRAIN resets the quota.
              </Notice>
            )}

            <div className="flex flex-col gap-3">
              <Tile
                label="Region"
                // Region, Delay and Universe are views of one set of markets, so the heading
                // picks or clears every one of them.
                any={anyMarketOn}
                onToggle={toggleMarkets}
                actions={
                  markets === 0 && (
                    <span className="text-body-compact text-status-warning">
                      Choose at least one
                    </span>
                  )
                }
              >
                <Spread columns={axes.regions.length}>
                  {axes.regions.map((axis) => (
                    <GroupChip
                      key={axis.value}
                      label={axis.value}
                      group={axis.group}
                      onChange={change}
                      title={
                        axis.value === REGION_AGNOSTIC_REGION ? REGION_AGNOSTIC_NOTE : undefined
                      }
                      className="w-full justify-center"
                    />
                  ))}
                </Spread>
              </Tile>
              <Tile label="Delay" any={anyMarketOn} onToggle={toggleMarkets}>
                <Spread columns={axes.delays.length} min="6rem">
                  {axes.delays.map((axis) => (
                    <GroupChip
                      key={axis.value}
                      label={`Delay ${axis.value}`}
                      group={axis.group}
                      onChange={change}
                      className="h-10 w-full justify-center"
                    />
                  ))}
                </Spread>
              </Tile>
              <Tile label="Universe" any={anyMarketOn} onToggle={toggleMarkets}>
                <Spread columns={7} min="6.5rem">
                  {axes.universes.map((axis) => (
                    <GroupChip
                      key={axis.value}
                      label={axis.value}
                      group={axis.group}
                      onChange={change}
                      className="w-full justify-center"
                    />
                  ))}
                </Spread>
              </Tile>
              <Tile
                label="Neutralization"
                any={anyNeutralizationOn}
                onToggle={() =>
                  setNeutralizations(
                    anyNeutralizationOn ? [] : allNeutralizations.map((n) => n.value),
                  )
                }
                actions={
                  neutralizations.length === 0 && (
                    <span className="text-body-compact text-status-warning">
                      Choose at least one
                    </span>
                  )
                }
              >
                <div className="grid gap-4 sm:grid-cols-2">
                  {splitNeutralizations(allNeutralizations).map(({ group, items }, at) => {
                    const ids = items.map((item) => item.value)
                    const any = ids.some((id) => neutralizations.includes(id))
                    return (
                      <div
                        key={group.id}
                        className={cn(
                          'flex min-w-0 flex-col gap-2',
                          at > 0 && 'sm:border-l sm:border-hairline sm:pl-4',
                        )}
                      >
                        <div className="flex min-h-6 items-center justify-between gap-3">
                          <ToggleTitle
                            label={group.id === 'risk' ? 'Risk' : 'Other'}
                            any={any}
                            onToggle={() =>
                              setNeutralizations((prev) =>
                                any
                                  ? prev.filter((v) => !ids.includes(v))
                                  : [...new Set([...prev, ...ids])],
                              )
                            }
                            className="text-caption tracking-wide text-ink-subtle uppercase"
                          />
                        </div>
                        <Spread columns={3} min="8rem">
                          {items.map((item) => (
                            <GroupChip
                              key={item.value}
                              label={item.label}
                              group={{
                                keys: [item.value],
                                on: neutralizations.includes(item.value) ? 1 : 0,
                              }}
                              onChange={(keys, on) =>
                                setNeutralizations((prev) =>
                                  on ? [...prev, ...keys] : prev.filter((k) => !keys.includes(k)),
                                )
                              }
                              className="w-full justify-center"
                            />
                          ))}
                        </Spread>
                      </div>
                    )
                  })}
                </div>
              </Tile>
              <Tile
                label="Investability"
                any={pairs.length > 0}
                onToggle={() => setPairs(pairs.length > 0 ? [] : allPairs.map(pairKey))}
                actions={
                  pairs.length === 0 && (
                    <span className="text-body-compact text-status-warning">
                      Choose at least one
                    </span>
                  )
                }
              >
                <Spread columns={allPairs.length} min="7rem">
                  {allPairs.map((pair) => (
                    <GroupChip
                      key={pairKey(pair)}
                      label={pairLabel(pair)}
                      group={{ keys: [pairKey(pair)], on: pairs.includes(pairKey(pair)) ? 1 : 0 }}
                      onChange={(keys, on) =>
                        setPairs((prev) =>
                          on ? [...prev, ...keys] : prev.filter((k) => !keys.includes(k)),
                        )
                      }
                      className="w-full justify-center"
                    />
                  ))}
                </Spread>
              </Tile>
              {/* A rule about Neutralization and Investability together. */}
              <Tile
                label="Market-Neutral"
                actions={
                  <span className="flex items-center gap-2.5">
                    {marketNeutralOnly && unhedged > 0 && (
                      <span className="text-body-compact leading-none whitespace-nowrap text-ink-muted tabular-nums">
                        {fmt.int(unhedged)} simulation{unhedged === 1 ? '' : 's'} skipped
                      </span>
                    )}
                    <Switch
                      checked={marketNeutralOnly}
                      onChange={setMarketNeutralOnly}
                      aria-label="Market-Neutral"
                    />
                  </span>
                }
              >
                <p className="text-body-compact whitespace-nowrap text-ink-subtle">
                  Skip None Neutralization without Max Trade or Max Position
                </p>
              </Tile>
            </div>

            <div className="flex flex-col gap-2 border-t border-hairline pt-4">
              <div className="flex items-baseline justify-between gap-3">
                <h3 className="text-body-compact font-medium tracking-wide text-ink-muted uppercase">
                  By market
                </h3>
                <Button size="sm" variant="ghost" onClick={() => reset(plan)}>
                  Reset to Defaults
                </Button>
              </div>
              <Tree branches={branches} onChange={change} />
            </div>
          </div>
        </Panel>
      ) : source && plan ? (
        <Panel>
          <Empty title="Nowhere to run it">
            No downloaded market holds every data field this expression reads.
          </Empty>
        </Panel>
      ) : (
        <Panel>
          <Empty title="Start with an Expression">
            Paste an Alpha Expression above, or switch to Alpha ID to run an existing Alpha with its
            own Settings.
          </Empty>
        </Panel>
      )}
    </Page>
  )
}
