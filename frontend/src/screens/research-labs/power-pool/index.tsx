/** LLM Power Pool Lab: an LLM writes Power Pool Alphas for your datasets while the task runs in Tasks. */

import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { useState } from 'react'
import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { DEFAULT_TRUNCATION } from '@/api/platform'
import { fmt } from '@/lib/format'
import { useCores } from '@/lib/preferences'
import { DEFAULT_SCOPE, useScopeOptions } from '@/lib/scope'
import { followOtherTabs } from '@/lib/tabs'
import { useProviderLabel } from '@/screens/ai/shared'
import { OperatorUsageDialog, operatorUsage } from '@/screens/dashboard/operators'
import type { FieldFilterState } from '@/screens/data/state'
import { AddTaskButtons, useAddTask } from '@/screens/research-labs/add-task'
import {
  MAX_SIMULATIONS,
  offeredOnly,
  simulationsValid,
  useLabMarket,
  useLabPreview,
} from '@/screens/research-labs/lab-task'
import { NeutralizationPicker } from '@/screens/research-labs/neutralization'
import { type PowerPoolRequest, powerPoolLab } from '@/screens/research-labs/power-pool/api'
import {
  CoresSetting,
  DatasetsPanel,
  MarketPanel,
  SimulationsSetting,
} from '@/screens/research-labs/task-settings'
import {
  Button,
  Checkbox,
  Chips,
  Disclosure,
  ErrorNotice,
  Field,
  Fieldset,
  LINK,
  Metric,
  Notice,
  Page,
  PageHeader,
  Panel,
  Textarea,
} from '@/ui/kit'
import { Select } from '@/ui/overlay'

interface PowerPoolDraft {
  region: string
  delay: number
  universe: string
  datasetIds: string[]
  fieldFilter: FieldFilterState | null
  /** `null` until chosen in the form: until then Settings' default applies. */
  cores: number | null
  simulations: number | null
  model: string | null
  onlyModel: boolean
  /** Empty keeps every neutralization BRAIN offers for the market. */
  neutralizations: string[]
  /** Empty draws from every downloaded universe of the market. */
  universes: string[]
  /** The user's own idea for every LLM call to test; empty lets it explore. */
  hypothesis: string
  /** Ask the LLM to use this quarter's unused operators where they fit. */
  favourUnused: boolean
}

const useDraft = create<PowerPoolDraft>()(
  persist(
    (): PowerPoolDraft => ({
      region: DEFAULT_SCOPE.region,
      delay: DEFAULT_SCOPE.delay,
      universe: DEFAULT_SCOPE.universe,
      datasetIds: [],
      fieldFilter: null,
      cores: null,
      simulations: null,
      model: null,
      onlyModel: false,
      neutralizations: [],
      universes: [],
      hypothesis: '',
      favourUnused: false,
    }),
    {
      name: 'alpha-harness-power-pool-lab',
      // Version 1 leaves cores unchosen, so Settings' default for new tasks applies.
      // Version 2 adds universes, none chosen: every downloaded one, as before.
      // Version 3 adds the hypothesis, empty: the LLM explores, as before.
      version: 3,
      migrate: (stored, version) => ({
        ...(stored as PowerPoolDraft),
        ...(version < 1 ? { cores: null } : {}),
        ...(version < 2 ? { universes: [] } : {}),
        hypothesis: '',
      }),
    },
  ),
)
followOtherTabs(useDraft)

const PRE =
  'num max-h-80 overflow-auto rounded-md border border-hairline bg-canvas p-3 text-body-compact whitespace-pre-wrap text-ink-muted'

export function PowerPoolLabScreen() {
  const draft = useDraft()
  const set = useDraft.setState
  const { scope, choose, move } = useLabMarket(draft, set, '/labs/power-pool')
  const options = useQuery({
    queryKey: ['power-pool-lab', 'options'],
    queryFn: powerPoolLab.options,
  })
  const models = options.data?.models ?? []
  const providerLabel = useProviderLabel()
  const model =
    draft.model && models.some((m) => m.ref === draft.model)
      ? draft.model
      : (options.data?.defaultModel ?? null)

  // BRAIN's legal list for this market; the LLM draws from whatever is chosen, or all of it.
  const scopeOptions = useScopeOptions({
    instrumentType: 'EQUITY',
    region: draft.region,
    delay: draft.delay,
    universe: draft.universe,
  })

  // The Dashboard's own read of the quarter, asked for only once the option is on.
  const usage = useQuery({ ...operatorUsage, enabled: draft.favourUnused })
  const unused = usage.data?.operators.filter((o) => o.alphas === 0).map((o) => o.name) ?? []
  const [showUsage, setShowUsage] = useState(false)
  // Never added without the operators it was asked to favour.
  const usageBlocked = !draft.favourUnused
    ? null
    : usage.isError
      ? "This quarter's operators could not be read. Untick Favour operators, or try again."
      : usage.data
        ? null
        : "Reading this quarter's operators from BRAIN…"

  const cores = useCores(draft.cores)
  const universes = offeredOnly(draft.universes, scopeOptions.universes)
  const body: PowerPoolRequest = {
    region: draft.region,
    delay: draft.delay,
    universe: draft.universe,
    dataset_ids: draft.datasetIds,
    field_filter: draft.fieldFilter ?? null,
    model,
    only_model: draft.onlyModel,
    neutralizations: offeredOnly(draft.neutralizations, scopeOptions.neutralizations),
    universes,
    cores,
    simulations: draft.simulations ?? 0,
    hypothesis: draft.hypothesis,
    favour_operators: draft.favourUnused ? unused : [],
  }
  const { preview, current } = useLabPreview('power-pool-lab', body, powerPoolLab.preview)
  const plan = preview.data
  const maxSimulations = options.data?.maxSimulations ?? MAX_SIMULATIONS
  const add = useAddTask(() => powerPoolLab.addTask(body))
  const ready =
    plan !== undefined &&
    current &&
    plan.problems.length === 0 &&
    simulationsValid(draft.simulations, maxSimulations) &&
    usageBlocked === null
  const blocked =
    usageBlocked ??
    plan?.problems[0] ??
    (draft.simulations === null ? 'Enter the simulations to run.' : null)

  return (
    <Page>
      <PageHeader
        sticky
        title="LLM Power Pool Lab"
        actions={<AddTaskButtons add={add} disabled={!ready} blocked={blocked} />}
      />
      {options.isError && <ErrorNotice error={options.error} title="Could not load the models" />}
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
      <MarketPanel scope={scope} onChange={move} />
      <DatasetsPanel
        ids={draft.datasetIds}
        scope={scope}
        onApply={(datasetIds, fieldFilter) => set({ datasetIds, fieldFilter })}
        onExplore={choose}
        filter={draft.fieldFilter}
        onClearFilter={() => set({ fieldFilter: null })}
        onRemove={(ids) => set({ datasetIds: draft.datasetIds.filter((x) => !ids.includes(x)) })}
      />
      <Panel title="Settings">
        <div className="flex flex-col gap-4">
          <div className="flex flex-wrap items-start gap-x-8 gap-y-4">
            <Fieldset legend="Model">
              <div className="flex flex-col gap-2">
                <Select
                  label="Model"
                  items={models.map((m) => ({
                    value: m.ref,
                    label: `${m.id} · ${providerLabel(m.provider)} · ${fmt.int(m.remainingToday)} left today`,
                  }))}
                  value={model}
                  onChange={(v) => set({ model: v })}
                />
                <Checkbox
                  label="Only use this model"
                  checked={draft.onlyModel}
                  onChange={(onlyModel) => set({ onlyModel })}
                />
                <p className="text-caption text-pretty text-ink-subtle">
                  {draft.onlyModel
                    ? 'When it runs out of requests for the day, the task waits for it.'
                    : "When it runs out of requests for the day or its provider is overloaded, the task carries on with the next model a Key can run. Every running task shares a model's daily requests."}
                </p>
              </div>
            </Fieldset>
            <CoresSetting value={cores} onChange={(next) => set({ cores: next })} />
            <SimulationsSetting
              value={draft.simulations}
              max={maxSimulations}
              placeholder="500"
              onChange={(next) => set({ simulations: next })}
            />
          </div>
          <Field
            label="Hypothesis · optional"
            hint="Every LLM call writes its Alphas to test this idea on the chosen datasets. Left empty, the LLM explores on its own."
          >
            <Textarea
              rows={3}
              maxLength={2000}
              placeholder="e.g. Stocks whose analysts keep revising estimates up outperform for the next month."
              value={draft.hypothesis}
              onChange={(e) => set({ hypothesis: e.target.value })}
            />
          </Field>
          <Fieldset legend="Operators">
            <div className="flex flex-col gap-1.5">
              <Checkbox
                label="Favour operators unused this quarter"
                checked={draft.favourUnused}
                onChange={(favourUnused) => set({ favourUnused })}
              />
              <p className="text-caption text-pretty text-ink-subtle">
                {draft.favourUnused && usage.data ? (
                  <>
                    <span className="num">{fmt.int(unused.length)}</span> of{' '}
                    <span className="num">{fmt.int(usage.data.operators.length)}</span> are unused
                    in {usage.data.label}. Every LLM call asks for them where they fit the idea.{' '}
                    <button type="button" className={LINK} onClick={() => setShowUsage(true)}>
                      See all
                    </button>
                  </>
                ) : (
                  "Total distinct operators over the quarter is one of BRAIN Genius's tiebreakers. On, the LLM is asked to reach for the ones your submitted Alphas have not used yet."
                )}
              </p>
              {draft.favourUnused && usage.isError && (
                <ErrorNotice
                  error={usage.error}
                  title="This quarter's operators could not be read"
                />
              )}
            </div>
          </Fieldset>
          {usage.data && (
            <OperatorUsageDialog usage={usage.data} open={showUsage} onOpenChange={setShowUsage} />
          )}
          {scopeOptions.universes.length > 0 && (
            <Fieldset legend={universes.length === 0 ? 'Universes · all downloaded' : 'Universes'}>
              <Chips
                label="Universes"
                items={scopeOptions.universes}
                value={draft.universes}
                onChange={(next) => set({ universes: next })}
              />
            </Fieldset>
          )}
          {scopeOptions.neutralizations.length > 0 && (
            <NeutralizationPicker
              available={scopeOptions.neutralizations}
              value={draft.neutralizations}
              onChange={(next) => set({ neutralizations: next })}
            />
          )}
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Metric boxed label="Datasets" value={fmt.int(draft.datasetIds.length)} />
            <Metric boxed label="Fields" value={fmt.int(plan?.fields)} />
            <Metric boxed label="LLM Calls" value={fmt.int(plan?.llmCalls)} hint="20 Alphas each" />
            <Metric
              boxed
              label="Universes"
              value={fmt.int(plan?.universes.length)}
              hint={`${fmt.int(plan?.neutralizations.length)} Neutralizations`}
            />
          </div>
          <p className="text-body-compact text-pretty text-ink-subtle">
            Each Alpha gets a random Universe, Neutralization and Decay; Truncation{' '}
            {DEFAULT_TRUNCATION}.
          </p>
          {preview.isError && <ErrorNotice error={preview.error} title="Could not plan the task" />}
          {plan?.problems.map((m) => (
            <Notice key={m} tone="error" title={m} />
          ))}
          {plan?.warnings.map((m) => (
            <Notice key={m} tone="warn" title={m} />
          ))}
          {plan?.prompt && (
            <Disclosure summary={`Prompt · ~${fmt.int(plan.prompt.tokens)} tokens`}>
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
