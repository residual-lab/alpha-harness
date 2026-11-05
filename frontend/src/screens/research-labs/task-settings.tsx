/** The Datasets and Settings panels of a lab task, and the task settings every lab asks for. */

import { DatabaseIcon, FilterIcon } from 'lucide-react'
import { useState } from 'react'
import { SIMULATION_SLOTS } from '@/api/platform'
import type { Scope } from '@/api/types'
import { DASH, fmt } from '@/lib/format'
import { useCores } from '@/lib/preferences'
import { isRegionAgnostic, regionLabel, useScopeOptions } from '@/lib/scope'
import { DatasetChips, useDatasetTree } from '@/screens/data/dataset-chips'
import { DatasetPicker } from '@/screens/data/dataset-picker'
import { describeFilter, type FieldFilterState, labFilter } from '@/screens/data/state'
import { NeutralizationPicker } from '@/screens/research-labs/neutralization'
import {
  Button,
  Chips,
  Disclosure,
  Empty,
  ErrorNotice,
  Field,
  Fieldset,
  Input,
  Metric,
  Notice,
  Panel,
  Segmented,
} from '@/ui/kit'
import { Sheet } from '@/ui/overlay'
import { ScopePicker } from '@/ui/scope-picker'
import type { LabDraft } from './lab-task'

const DECAYS = [0, 3, 5, 7, 10]

/** A task may hold every slot BRAIN allows. */
const CORES = Array.from({ length: SIMULATION_SLOTS }, (_, i) => ({ value: i + 1, label: i + 1 }))

export function CoresSetting({
  value,
  onChange,
}: {
  value: number
  onChange: (cores: number) => void
}) {
  return (
    <Fieldset legend="Cores">
      <Segmented label="Cores" items={CORES} value={value} onChange={onChange} />
    </Fieldset>
  )
}

/** Reports `null` while the field is empty. */
export function SimulationsSetting({
  value,
  max,
  placeholder,
  onChange,
}: {
  value: number | null
  max: number
  placeholder?: string
  onChange: (simulations: number | null) => void
}) {
  // Said here, beside the box, so every lab's Run greys out with its reason in view.
  const over = value !== null && value > max
  return (
    <Field
      label="Simulations"
      error={over ? `A task takes at most ${fmt.int(max)} simulations.` : undefined}
    >
      <Input
        type="number"
        min={1}
        max={max}
        step={1}
        placeholder={placeholder}
        aria-invalid={over || undefined}
        className="w-32"
        value={value ?? ''}
        onChange={(e) => {
          const n = Number(e.target.value)
          onChange(e.target.value === '' || !Number.isFinite(n) ? null : Math.max(0, Math.floor(n)))
        }}
      />
    </Field>
  )
}

/** What either lab's preview says about a task. */
export interface LabPlan {
  fields: { total: number; matrix: number; vector: number }
  leftOut: { vector: number }
  universes: string[]
  sample: { expression: string; settings: Record<string, unknown> }[]
  problems: string[]
  warnings: string[]
}

/** The market a lab's task runs in. Another region or delay starts its datasets afresh. */
export function MarketPanel({
  scope,
  onChange,
}: {
  scope: Scope
  onChange: (change: Partial<Scope>) => void
}) {
  return (
    <Panel
      title="Market"
      description="Where every Alpha in the task runs. Datasets are chosen within it."
    >
      <ScopePicker scope={scope} onChange={onChange} />
    </Panel>
  )
}

export function DatasetsPanel({
  ids,
  scope,
  onApply,
  onExplore,
  onRemove,
  filter,
  onClearFilter,
}: {
  ids: string[]
  /** The market the datasets belong to, which places each under its category. */
  scope: Scope
  /** Chosen in the sheet, without leaving the lab. */
  onApply: (ids: string[], filter: FieldFilterState | null) => void
  /** On to the Data Explorer with what the sheet holds, for the filters it has no room for. */
  onExplore: (ids: string[], filter: FieldFilterState | null) => void
  onRemove: (ids: string[]) => void
  /** The Data Explorer's filter the datasets were chosen under, which narrows their fields. */
  filter: FieldFilterState | null | undefined
  onClearFilter: () => void
}) {
  const chosen = ids.length > 0
  const { tree, nameOf, ready } = useDatasetTree(chosen ? scope : null)
  const [picking, setPicking] = useState(false)
  const onChoose = () => setPicking(true)
  return (
    <Panel
      title="Datasets"
      actions={
        chosen && (
          <Button size="sm" onClick={onChoose}>
            <DatabaseIcon />
            Choose Datasets
          </Button>
        )
      }
    >
      {picking && (
        <DatasetsSheet
          scope={scope}
          ids={ids}
          filter={filter ?? null}
          onClose={() => setPicking(false)}
          onApply={(next, narrowed) => {
            setPicking(false)
            onApply(next, narrowed)
          }}
          onExplore={onExplore}
        />
      )}
      {chosen ? (
        <div className="flex flex-col gap-3">
          <DatasetChips tree={tree} value={ids} nameOf={nameOf} onRemove={onRemove} ready={ready} />
          {filter && (
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-body-compact text-ink-subtle">
              <FilterIcon className="size-3.5 shrink-0" aria-hidden />
              <span className="min-w-0">
                Only fields matching {describeFilter(filter, scope.region).join(' \u00b7 ')}
              </span>
              <Button size="sm" variant="ghost" onClick={onClearFilter}>
                Use All Fields
              </Button>
            </div>
          )}
        </div>
      ) : (
        <Empty title="No datasets chosen" icon={<DatabaseIcon />}>
          <Button className="mt-2" onClick={onChoose}>
            <DatabaseIcon />
            Choose Datasets
          </Button>
        </Empty>
      )}
    </Panel>
  )
}

function DatasetsSheet({
  scope,
  ids,
  filter,
  onClose,
  onApply,
  onExplore,
}: {
  scope: Scope
  ids: string[]
  filter: FieldFilterState | null
  onClose: () => void
  onApply: (ids: string[], filter: FieldFilterState | null) => void
  onExplore: (ids: string[], filter: FieldFilterState | null) => void
}) {
  const [datasetIds, setDatasetIds] = useState(ids)
  const [narrowed, setNarrowed] = useState<FieldFilterState>(filter ?? {})
  // `null` when nothing narrows, as the Data Explorer hands a filter back.
  const kept = () => labFilter(narrowed)
  return (
    <Sheet
      open
      onOpenChange={(open) => !open && onClose()}
      title="Datasets"
      description="The lab writes its Alphas from the fields these leave."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            disabled={datasetIds.length === 0}
            onClick={() => onApply(datasetIds, kept())}
          >
            Apply
          </Button>
        </>
      }
    >
      <DatasetPicker
        scope={scope}
        datasetIds={datasetIds}
        setDatasetIds={setDatasetIds}
        filter={narrowed}
        setFilter={setNarrowed}
        onMoreFilters={() => onExplore(datasetIds, kept())}
      />
    </Sheet>
  )
}

export function SettingsPanel({
  draft,
  set,
  vector,
  chosenVector,
  decays = DECAYS,
  maxSimulations,
  suggested,
  plan,
  error,
}: {
  draft: LabDraft
  /** What the task runs while the Simulations box is empty. */
  suggested?: number | null
  set: (change: Partial<LabDraft>) => void
  vector: string[]
  chosenVector: string[]
  decays?: number[] | undefined
  maxSimulations: number
  plan: LabPlan | undefined
  error: unknown
}) {
  const simulations = draft.simulations
  const cores = useCores(draft.cores)
  const showVector = (plan?.fields.vector ?? 0) > 0 || (plan?.leftOut.vector ?? 0) > 0
  // BRAIN's own legal list for this market, which is wider than the four a lab searches by
  // default — picking any of them is what tells the lab to search those instead.
  const { neutralizations } = useScopeOptions({
    instrumentType: 'EQUITY',
    region: draft.region,
    delay: draft.delay,
    universe: draft.universe,
  })

  return (
    <Panel title="Settings">
      <div className="flex flex-col gap-4">
        <div className="flex flex-wrap items-start gap-x-8 gap-y-4">
          <CoresSetting value={cores} onChange={(next) => set({ cores: next })} />
          <SimulationsSetting
            value={simulations}
            max={maxSimulations}
            {...(suggested ? { placeholder: String(suggested) } : {})}
            onChange={(next) => set({ simulations: next })}
          />
          <Fieldset legend="Decay">
            <Segmented
              label="Decay"
              items={decays.map((v) => ({ value: v, label: v }))}
              value={draft.decay}
              onChange={(decay) => set({ decay })}
            />
          </Fieldset>
          {showVector && (
            <Fieldset legend="Vector Operators">
              <Chips
                label="Vector Operators"
                items={vector.map((op) => ({ value: op, label: op }))}
                value={chosenVector}
                onChange={(ops) => set({ vectorOperators: ops })}
              />
            </Fieldset>
          )}
        </div>
        {neutralizations.length > 0 && (
          <NeutralizationPicker
            available={neutralizations}
            value={draft.neutralizations}
            onChange={(next) => set({ neutralizations: next })}
          />
        )}
        <div className="grid gap-3 sm:grid-cols-3">
          <Metric boxed label="Market" value={`${regionLabel(draft.region)} · D${draft.delay}`} />
          <Metric boxed label="Fields" value={fmt.int(plan?.fields.total)} />
          <Metric boxed label="Universes" value={fmt.int(plan?.universes.length)} />
        </div>
        {isRegionAgnostic(draft) && (
          <Notice tone="info" title="Every alpha here runs in four regions at once">
            One simulation covers USA, Europe, Asia and Global, and spends four of today's
            allowance. The alphas it makes can be submitted where two or more of those regions hold
            up.
          </Notice>
        )}
        {error ? <ErrorNotice error={error} title="Could not plan the task" /> : null}
        {plan?.problems.map((m) => (
          <Notice key={m} tone="error" title={m} />
        ))}
        {plan?.warnings.map((m) => (
          <Notice key={m} tone="warn" title={m} />
        ))}
        {plan && plan.sample.length > 0 && (
          <Disclosure summary="Sample Alphas">
            <ul className="flex flex-col gap-2">
              {plan.sample.map((s, i) => (
                <li
                  key={i}
                  className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5"
                >
                  <code className="num text-body-compact break-all text-ink">{s.expression}</code>
                  <span className="text-body-compact text-ink-subtle">
                    {String(s.settings['universe'] ?? DASH)} ·{' '}
                    {String(s.settings['neutralization'] ?? DASH)}
                  </span>
                </li>
              ))}
            </ul>
          </Disclosure>
        )}
      </div>
    </Panel>
  )
}
