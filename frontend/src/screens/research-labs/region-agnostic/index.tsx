/**
 * Region Agnostic Lab: an LLM writes Alphas from region-agnostic fields, and each runs as an
 * ordinary simulation in every chosen region that carries its fields, the settings the same
 * but for the region. BRAIN runs two region-agnostic simulations at a time; this runs none.
 */

import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { useEffect } from 'react'
import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { fmt } from '@/lib/format'
import { useCores } from '@/lib/preferences'
import { REGION_AGNOSTIC, useScopeOptions } from '@/lib/scope'
import { useProviderLabel } from '@/screens/ai/shared'
import type { PickedField } from '@/screens/data/dataset-pick'
import type { FieldFilterState } from '@/screens/data/state'
import { PromptPicker, useChosenPrompt } from '@/screens/prompts/picker'
import { AddTaskButtons, useAddTask } from '@/screens/research-labs/add-task'
import {
  fieldIdsOf,
  MAX_SIMULATIONS,
  simulationsValid,
  useLabMarket,
  useLabPreview,
} from '@/screens/research-labs/lab-task'
import { NeutralizationPicker } from '@/screens/research-labs/neutralization'
import {
  type RegionAgnosticRequest,
  regionAgnosticLab,
} from '@/screens/research-labs/region-agnostic/api'
import {
  CoresSetting,
  DatasetsPanel,
  SimulationsSetting,
} from '@/screens/research-labs/task-settings'
import {
  Button,
  Chips,
  Disclosure,
  ErrorNotice,
  Fieldset,
  Metric,
  Notice,
  Page,
  PageHeader,
  Panel,
  Segmented,
} from '@/ui/kit'
import { Select } from '@/ui/overlay'

const SIZES = ['LARGE', 'MEDIUM', 'SMALL'] as const
type Size = (typeof SIZES)[number]

/** The built-in this lab sends, and the kind of every saved prompt it can send instead. */
const PROMPT_KIND = 'region_agnostic_lab'

interface RegionAgnosticDraft {
  /** Always the All Regions market: that is where region-agnostic fields are chosen. */
  region: string
  delay: number
  universe: string
  datasetIds: string[]
  /** Unused here: fields are chosen by id, but every lab's market has one. */
  fieldFilter: FieldFilterState | null
  fields?: PickedField[]
  rankBy?: string | null
  regions: string[]
  /** BRAIN's region-agnostic universe size; each region's universe follows from it. */
  size?: Size
  /** `null` until chosen in the form: until then Settings' default applies. */
  cores: number | null
  simulations: number | null
  model: string | null
  promptId: number | null
  neutralizations: string[]
}

const useDraft = create<RegionAgnosticDraft>()(
  persist(
    (): RegionAgnosticDraft => ({
      region: REGION_AGNOSTIC,
      delay: 1,
      universe: '',
      datasetIds: [],
      fieldFilter: null,
      fields: [],
      rankBy: null,
      regions: ['USA', 'EUR', 'ASI', 'GLB'],
      size: 'LARGE',
      cores: null,
      simulations: null,
      model: null,
      promptId: null,
      neutralizations: [],
    }),
    { name: 'alpha-harness-region-agnostic-lab' },
  ),
)

const PRE =
  'num max-h-80 overflow-auto rounded-md border border-hairline bg-canvas p-3 text-body-compact whitespace-pre-wrap text-ink-muted'

export function RegionAgnosticLabScreen() {
  const draft = useDraft()
  const set = useDraft.setState
  const { panel } = useLabMarket(draft, set, '/labs/region-agnostic')
  const options = useQuery({
    queryKey: ['region-agnostic-lab', 'options', draft.delay],
    queryFn: () => regionAgnosticLab.options(draft.delay),
  })
  const allRegions = useScopeOptions({
    instrumentType: 'EQUITY',
    region: REGION_AGNOSTIC,
    delay: draft.delay,
    universe: draft.universe,
  })
  // The All Regions market's universe the fields are picked in: the first BRAIN offers.
  const firstUniverse = allRegions.universes[0]?.value
  useEffect(() => {
    if (!draft.universe && firstUniverse) set({ universe: firstUniverse })
  }, [draft.universe, firstUniverse])

  const models = options.data?.models ?? []
  const providerLabel = useProviderLabel()
  const cores = useCores(draft.cores)
  const model =
    draft.model && models.some((m) => m.ref === draft.model)
      ? draft.model
      : (options.data?.defaultModel ?? null)
  const promptId = useChosenPrompt(PROMPT_KIND, draft.promptId ?? null)
  const offered = options.data?.regions ?? []
  const regions = draft.regions.filter((r) => offered.some((o) => o.region === r))
  // BRAIN's region-agnostic universe setting: one size, a universe per region.
  const size = draft.size ?? 'LARGE'
  const universeOf = (region: string) => options.data?.sizes[size]?.[region] ?? ''
  const shared = offered
    .filter((o) => regions.includes(o.region))
    .map((o) => o.neutralizations)
    .reduce<string[] | null>(
      (acc, list) => (acc ? acc.filter((n) => list.includes(n)) : list),
      null,
    )

  const body: RegionAgnosticRequest = {
    delay: draft.delay,
    universe: draft.universe,
    regions,
    size,
    field_ids: fieldIdsOf(draft),
    dataset_ids: draft.datasetIds,
    rank_by: draft.fields?.length ? (draft.rankBy ?? null) : null,
    model,
    prompt_id: promptId,
    neutralizations: draft.neutralizations,
    cores,
    simulations: draft.simulations ?? 0,
  }
  const { preview, current } = useLabPreview('region-agnostic-lab', body, regionAgnosticLab.preview)
  const plan = preview.data
  const maxSimulations = options.data?.maxSimulations ?? MAX_SIMULATIONS
  const add = useAddTask(() => regionAgnosticLab.addTask(body))
  const ready =
    plan !== undefined &&
    current &&
    plan.problems.length === 0 &&
    simulationsValid(draft.simulations, maxSimulations)

  return (
    <Page>
      <PageHeader
        title="Region Agnostic Lab"
        description="Region-agnostic fields, written into Alphas by an LLM and run as ordinary simulations in every region that carries them: the same settings but the region, ten to a core. Results come back grouped by Alpha, a row per region."
        actions={<AddTaskButtons add={add} disabled={!ready} />}
      />
      {options.isError && <ErrorNotice error={options.error} title="Could not load the options" />}
      {options.isSuccess && models.length === 0 && (
        <Notice
          tone="warn"
          title="This lab needs a model"
          action={
            <Button size="sm" render={<Link to="/ai/$tab" params={{ tab: 'models' }} />}>
              Set up a model
            </Button>
          }
        >
          Add a Key in LLM Integration and set up a model for it, with the limits your provider
          shows you.
        </Notice>
      )}
      <DatasetsPanel {...panel}>
        <div className="flex flex-col gap-4">
          <p className="text-body-compact text-pretty text-ink-subtle">
            Choose Datasets or Fields opens the All Regions market. There, More filters has Regions
            per field: keep to fields two or more regions carry, then tick the ones to use.
          </p>
          <Fieldset
            legend="Prompt"
            hint="What the LLM is told first. Edits in LLM Prompts reach running tasks on their next call."
          >
            <PromptPicker
              kind={PROMPT_KIND}
              value={promptId}
              onChange={(next) => set({ promptId: next })}
            />
          </Fieldset>
        </div>
      </DatasetsPanel>

      <Panel title="Regions">
        <div className="flex flex-col gap-4">
          <div className="flex flex-wrap items-start gap-x-8 gap-y-4">
            <Fieldset legend="Delay">
              <Segmented
                label="Delay"
                items={[
                  { value: 0, label: '0' },
                  { value: 1, label: '1' },
                ]}
                value={draft.delay}
                onChange={(delay) => set({ delay, universe: '' })}
              />
            </Fieldset>
            <Fieldset
              legend="Universe"
              hint="BRAIN's region-agnostic sizes, each a universe per region."
            >
              <Segmented
                label="Universe"
                items={SIZES.map((v) => ({ value: v, label: v }))}
                value={size}
                onChange={(next) => set({ size: next })}
              />
            </Fieldset>
            <Fieldset
              legend="Run in"
              hint="Each Alpha runs in every one of these that carries all its fields: two at least."
            >
              <Chips
                label="Regions"
                items={offered.map((o) => ({ value: o.region, label: o.region }))}
                value={regions}
                onChange={(next) => set({ regions: next })}
              />
            </Fieldset>
          </div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {regions.map((region) => {
              const o = offered.find((x) => x.region === region)
              const count = plan?.perRegion.find((p) => p.region === region)?.fields
              return (
                <Fieldset
                  key={region}
                  legend={`${region} universe`}
                  hint={count === undefined ? undefined : `${fmt.int(count)} of the fields here`}
                >
                  <span className="num text-body text-ink">{universeOf(region) || '—'}</span>
                  {o && !o.universes.includes(universeOf(region)) && (
                    <span className="text-body-compact text-pnl-negative">Not downloaded</span>
                  )}
                </Fieldset>
              )
            })}
          </div>
        </div>
      </Panel>

      <Panel title="Settings">
        <div className="flex flex-col gap-4">
          <div className="flex flex-wrap items-start gap-x-8 gap-y-4">
            <Fieldset legend="Model">
              <Select
                label="Model"
                items={models.map((m) => ({
                  value: m.ref,
                  label: `${m.id} · ${providerLabel(m.provider)} · ${fmt.int(m.remainingToday)} left today`,
                }))}
                value={model}
                onChange={(v) => set({ model: v })}
              />
            </Fieldset>
            <CoresSetting value={cores} onChange={(next) => set({ cores: next })} />
            <SimulationsSetting
              value={draft.simulations}
              max={maxSimulations}
              placeholder="500"
              onChange={(next) => set({ simulations: next })}
            />
          </div>
          {shared && shared.length > 0 && (
            <NeutralizationPicker
              available={shared.map((n) => ({
                value: n,
                // BRAIN's own label where the All Regions market names it.
                label: allRegions.neutralizations.find((c) => c.value === n)?.label ?? n,
              }))}
              value={draft.neutralizations}
              onChange={(next) => set({ neutralizations: next })}
              hint="Only what every chosen region offers. None chosen draws from all of them; each Alpha gets one, the same in every region."
            />
          )}
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Metric boxed label="Fields" value={fmt.int(plan?.fields)} hint="Chosen, ranked" />
            <Metric boxed label="Regions" value={fmt.int(regions.length)} />
            <Metric
              boxed
              label="LLM Calls"
              value={fmt.int(plan?.llmCalls)}
              hint="20 Alphas each, a simulation per region"
            />
            <Metric
              boxed
              label="Single-region fields"
              value={fmt.int(plan?.lonely)}
              hint="Need another field beside them"
            />
          </div>
          {preview.isError && <ErrorNotice error={preview.error} title="Could not plan the task" />}
          {plan?.problems.map((m) => (
            <Notice key={m} tone="error" title={m} />
          ))}
          {plan?.warnings.map((m) => (
            <Notice key={m} tone="warn" title={m} />
          ))}
          {plan?.prompt && (
            <Disclosure
              summary={`Prompt · ${plan.prompt.name} · ~${fmt.int(plan.prompt.tokens)} tokens`}
            >
              <div className="flex flex-col gap-2">
                <pre className={PRE} role="region" aria-label="System prompt">
                  {plan.prompt.system}
                </pre>
                <pre className={PRE} role="region" aria-label="User prompt">
                  {plan.prompt.user}
                </pre>
              </div>
            </Disclosure>
          )}
        </div>
      </Panel>
    </Page>
  )
}
