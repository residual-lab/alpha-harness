/** Super Alpha Lab: SuperAlphas built from your own submitted Alphas, every pairing in one task.
 *  The built-in selections and combos sit beside the user's own, kept on this computer. */

import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { PencilIcon, PlusIcon, Trash2Icon } from 'lucide-react'
import { useState } from 'react'
import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import type { components } from '@/api/generated'
import { http } from '@/api/http'
import {
  REGION_AGNOSTIC_REGION,
  SUPER_ALPHA_MIN_SELECTION,
  SUPER_ALPHA_SLOTS,
} from '@/api/platform'
import { cn } from '@/lib/cn'
import { fmt } from '@/lib/format'
import { keepInHarness } from '@/lib/preferences'
import { useScope, useScopeOptions } from '@/lib/scope'
import { followOtherTabs } from '@/lib/tabs'
import { AddTaskButtons, useAddTask } from '@/screens/research-labs/add-task'
import {
  Button,
  Checkbox,
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
  Textarea,
} from '@/ui/kit'
import { Select } from '@/ui/overlay'
import { ScopePicker } from '@/ui/scope-picker'

type Schemas = components['schemas']
type Activation = Schemas['SuperTask']['activation']

const ACTIVATIONS: { value: NonNullable<Activation>; label: string }[] = [
  { value: 'BOTH', label: 'IS and OS' },
  { value: 'IS', label: 'IS' },
  { value: 'OS', label: 'OS' },
]
const CORES = Array.from({ length: SUPER_ALPHA_SLOTS }, (_, i) => ({
  value: i + 1,
  label: String(i + 1),
}))
const MAX_LIMIT = 200

type Kind = 'selections' | 'combos'
interface Own {
  id: string
  name: string
  code: string
}
interface Recipes {
  selections: Own[]
  combos: Own[]
  /** Unticked, by `builtin:{name}` or `custom:{id}`; everything else runs. */
  off: string[]
}

/** The user's own selections and combos, and which of every one are left out. */
const useRecipes = create<Recipes>()(
  persist((): Recipes => ({ selections: [], combos: [], off: [] }), {
    name: 'alpha-harness-super-lab',
    version: 1,
  }),
)
followOtherTabs(useRecipes)
const useKeptRecipes = keepInHarness(
  useRecipes,
  '/api/preferences/super-lab',
  'selections and combos',
)

const STARTER: Record<Kind, string> = {
  selections: '(operator_count <= 100) * (1 - prod_correlation)',
  combos: 'stats = generate_stats(alpha);\nmax(ts_ir(stats.returns, 120), 0)',
}
const SINGULAR: Record<Kind, string> = { selections: 'Selection', combos: 'Combo' }

function addOwn(kind: Kind, name: string, code: string) {
  const id = crypto.randomUUID()
  useRecipes.setState((s) => ({ [kind]: [...s[kind], { id, name, code }] }))
  return id
}

function updateOwn(kind: Kind, id: string, change: Partial<Own>) {
  useRecipes.setState((s) => ({
    [kind]: s[kind].map((r) => (r.id === id ? { ...r, ...change } : r)),
  }))
}

function removeOwn(kind: Kind, id: string) {
  useRecipes.setState((s) => ({
    [kind]: s[kind].filter((r) => r.id !== id),
    off: s.off.filter((k) => k !== `custom:${id}`),
  }))
}

function toggle(key: string) {
  useRecipes.setState((s) => ({
    off: s.off.includes(key) ? s.off.filter((k) => k !== key) : [...s.off, key],
  }))
}

/** What a task crosses of one kind: the built-in ones and the user's own, as ticked. */
function chosen(builtins: Schemas['Recipe'][], own: Own[], off: string[]) {
  return [
    ...builtins.filter((r) => !off.includes(`builtin:${r.name}`)),
    ...own
      .filter((r) => !off.includes(`custom:${r.id}`))
      .map((r) => ({ name: r.name.trim(), code: r.code.trim() })),
  ]
}

/** Why a list cannot run as ticked, or `null`: BRAIN needs both halves, and results are named
 *  for them, so a blank one or two of one name would read as one. */
function problemOf(kind: Kind, list: { name: string; code: string }[]) {
  const word = SINGULAR[kind].toLowerCase()
  if (list.length === 0) return `Tick at least one ${word}.`
  if (list.some((r) => !r.name || !r.code)) return `Every ticked ${word} needs a name and code.`
  if (new Set(list.map((r) => r.name)).size < list.length)
    return `Two ticked ${word}s share a name: rename one.`
  return null
}

export function SuperAlphaLabScreen() {
  const [scope, setScope] = useScope('/labs/super-alpha')
  const options = useScopeOptions(scope)
  const [picked, setPicked] = useState('')
  const [limit, setLimit] = useState(30)
  const [activation, setActivation] = useState<NonNullable<Activation>>('BOTH')
  const [cores, setCores] = useState(3)
  const recipes = useRecipes()
  const kept = useKeptRecipes()
  /** The selection just written from the panel's lead, focused so it is seen below the rest. */
  const [fresh, setFresh] = useState<string | null>(null)

  // BRAIN's own list for the market; Subindustry first where it has one, as most Alphas use it.
  const neutralization = options.neutralizations.some((n) => n.value === picked)
    ? picked
    : (options.neutralizations.find((n) => n.value === 'SUBINDUSTRY')?.value ??
      options.neutralizations[0]?.value ??
      '')
  const agnostic = scope.region === REGION_AGNOSTIC_REGION
  const limitValid =
    Number.isInteger(limit) && limit >= SUPER_ALPHA_MIN_SELECTION && limit <= MAX_LIMIT
  const settings = {
    region: scope.region,
    delay: scope.delay,
    universe: scope.universe,
    neutralization,
    selectionLimit: limit,
    activation,
    cores,
  }

  const plan = useQuery({
    queryKey: ['super-lab', 'plan', limitValid ? limit : SUPER_ALPHA_MIN_SELECTION, activation],
    queryFn: () =>
      http.post<Schemas['SuperPlan']>('/api/super-lab/plan', {
        ...settings,
        selectionLimit: limitValid ? limit : SUPER_ALPHA_MIN_SELECTION,
      }),
    placeholderData: keepPreviousData,
  })
  const selections = chosen(plan.data?.selections ?? [], recipes.selections, recipes.off)
  const combos = chosen(plan.data?.combos ?? [], recipes.combos, recipes.off)
  const problem = problemOf('selections', selections) ?? problemOf('combos', combos)
  const simulations =
    selections.length *
    combos.length *
    (plan.data?.decays.length ?? 0) *
    (activation === 'BOTH' ? 2 : 1)
  const add = useAddTask(() =>
    http.post<Schemas['AddedTask']>('/api/super-lab/tasks', { ...settings, selections, combos }),
  )
  const ready =
    !agnostic &&
    limitValid &&
    options.ready &&
    neutralization !== '' &&
    plan.data !== undefined &&
    problem === null

  return (
    <Page>
      <PageHeader
        sticky
        title="Super Alpha Lab"
        description="Combines your submitted Alphas into SuperAlphas, ready to submit"
        actions={<AddTaskButtons add={add} disabled={!ready} blocked={problem} />}
      />

      <Panel
        title="Market"
        description="Only your submitted ACTIVE Alphas of this region and delay can be selected."
      >
        <div className="flex flex-col gap-4">
          <ScopePicker scope={scope} onChange={setScope} />
          {agnostic && (
            <Notice
              tone="error"
              title="A SuperAlpha combines Alphas of one region. Pick a region other than All Regions."
            />
          )}
          <Field label="Neutralization" className="max-w-xs">
            <Select
              label="Neutralization"
              items={options.neutralizations}
              value={neutralization}
              onChange={setPicked}
            />
          </Field>
        </div>
      </Panel>

      <Panel
        title="Settings"
        description="Selection Handling is Positive: an Alpha a selection scores zero is left out."
      >
        <div className="flex flex-wrap items-end gap-6">
          <Field
            label="Selection Limit"
            hint={`Alphas kept from the top of each ranking, ${SUPER_ALPHA_MIN_SELECTION} to ${MAX_LIMIT}.`}
            className="w-48"
          >
            <Input
              type="number"
              min={SUPER_ALPHA_MIN_SELECTION}
              max={MAX_LIMIT}
              value={Number.isNaN(limit) ? '' : String(limit)}
              onChange={(e) => setLimit(e.target.valueAsNumber)}
              className="num"
            />
          </Field>
          <Fieldset legend="Component Activation">
            <Segmented
              label="Component Activation"
              items={ACTIVATIONS}
              value={activation}
              onChange={setActivation}
            />
          </Fieldset>
          <Fieldset legend="Cores">
            <Segmented label="Cores" items={CORES} value={cores} onChange={setCores} />
          </Fieldset>
        </div>
        {!limitValid && (
          <Notice
            tone="error"
            title={`Selection Limit must be a whole number from ${SUPER_ALPHA_MIN_SELECTION} to ${MAX_LIMIT}.`}
            className="mt-3"
          />
        )}
      </Panel>

      <Panel
        title="What Runs"
        description="Every ticked selection with every ticked combo, at two Decays to land Turnover between 2% and 40%. Each runs in Full mode, so BRAIN's own checks say which are submittable: those show green in Tasks. Add your own, or copy a built-in one to edit; yours are kept on this computer."
        actions={
          <Button
            title="A selection scores each of your own submitted Alphas, and each SuperAlpha keeps the top ones up to the Selection Limit."
            onClick={() =>
              setFresh(
                addOwn(
                  'selections',
                  `My Selection ${recipes.selections.length + 1}`,
                  STARTER.selections,
                ),
              )
            }
          >
            <PencilIcon />
            Write Your Own Selection
          </Button>
        }
      >
        {kept.isError && (
          <ErrorNotice
            className="mb-4"
            error={kept.error}
            title="Could not load your own selections and combos"
          />
        )}
        {plan.isError && <ErrorNotice error={plan.error} title="Could not plan the task" />}
        {plan.data && (
          <div className="flex flex-col gap-4">
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
              <Metric boxed label="SuperAlpha Simulations" value={fmt.int(simulations)} />
              <Metric
                boxed
                label="Decay"
                value={plan.data.decays.map((d) => String(d)).join(' and ')}
              />
              <Metric boxed label="Truncation" value={String(plan.data.truncation)} />
            </div>
            {problem && <Notice tone="warn" title={problem} />}
            <RecipeList
              kind="selections"
              builtins={plan.data.selections}
              recipes={recipes}
              fresh={fresh}
            />
            <RecipeList kind="combos" builtins={plan.data.combos} recipes={recipes} />
            <Notice
              tone="info"
              title="Each selection caps an Alpha's operators at 8,000 divided by the Selection Limit, so the selected Alphas never pass BRAIN's 8,000-operator ceiling. Submitting stays on BRAIN, with a description of the selection and the combo."
            />
          </div>
        )}
      </Panel>
    </Page>
  )
}

function RecipeList({
  kind,
  builtins,
  recipes,
  fresh,
}: {
  kind: Kind
  builtins: Schemas['Recipe'][]
  recipes: Recipes
  /** An own one just added from elsewhere on the screen, to focus. */
  fresh?: string | null
}) {
  const own = recipes[kind]
  const ticked =
    builtins.filter((r) => !recipes.off.includes(`builtin:${r.name}`)).length +
    own.filter((r) => !recipes.off.includes(`custom:${r.id}`)).length
  const card = (on: boolean) =>
    cn(
      'flex flex-col gap-2 rounded-md border p-3 transition-colors',
      on ? 'border-primary bg-surface-2' : 'border-hairline bg-surface-1',
    )
  return (
    <section className="flex flex-col gap-2">
      <h3 className="text-body font-medium text-ink">
        {kind === 'selections' ? 'Selections' : 'Combos'}{' '}
        <span className="num font-normal text-ink-subtle">
          {fmt.int(ticked)} of {fmt.int(builtins.length + own.length)} ticked
        </span>
      </h3>
      <ul className="flex flex-col gap-2">
        {builtins.map((r) => {
          const on = !recipes.off.includes(`builtin:${r.name}`)
          return (
            <li key={r.name} className={card(on)}>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <Checkbox
                  checked={on}
                  onChange={() => toggle(`builtin:${r.name}`)}
                  label={<span className="font-medium text-ink">{r.name}</span>}
                />
                <Button
                  size="sm"
                  variant="ghost"
                  title={`Add a copy of this ${SINGULAR[kind].toLowerCase()} to edit as your own`}
                  onClick={() => addOwn(kind, `${r.name} (edited)`, r.code)}
                >
                  <PencilIcon />
                  Edit a Copy
                </Button>
              </div>
              <code className="num text-body-compact break-all whitespace-pre-wrap text-ink-muted">
                {r.code}
              </code>
            </li>
          )
        })}
        {own.map((r) => {
          const on = !recipes.off.includes(`custom:${r.id}`)
          return (
            <li key={r.id} className={card(on)}>
              <div className="flex items-center gap-2">
                <Checkbox
                  checked={on}
                  onChange={() => toggle(`custom:${r.id}`)}
                  label={null}
                  aria-label={`Run ${r.name}`}
                />
                <Input
                  aria-label={`${SINGULAR[kind]} name`}
                  className="max-w-md flex-1 font-medium"
                  maxLength={120}
                  value={r.name}
                  onChange={(e) => updateOwn(kind, r.id, { name: e.target.value })}
                />
                <Button
                  size="icon-sm"
                  variant="ghost"
                  aria-label={`Delete ${r.name}`}
                  title={`Delete this ${SINGULAR[kind].toLowerCase()}`}
                  onClick={() => removeOwn(kind, r.id)}
                >
                  <Trash2Icon />
                </Button>
              </div>
              <Textarea
                aria-label={`${r.name} code`}
                autoFocus={r.id === fresh}
                className="font-mono"
                maxLength={8000}
                rows={Math.min(8, Math.max(2, r.code.split('\n').length + 1))}
                spellCheck={false}
                value={r.code}
                onChange={(e) => updateOwn(kind, r.id, { code: e.target.value })}
              />
            </li>
          )
        })}
      </ul>
      <div>
        <Button
          variant="secondary"
          onClick={() => addOwn(kind, `My ${SINGULAR[kind]} ${own.length + 1}`, STARTER[kind])}
        >
          <PlusIcon />
          Add {SINGULAR[kind]}
        </Button>
      </div>
    </section>
  )
}
