/**
 * The Variables panel: one row per `$name` the template writes, each defined as fields from
 * chosen datasets or as typed values, with what it holds in this market beside it.
 */

import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { DatabaseIcon, PencilIcon, RotateCcwIcon } from 'lucide-react'
import { useState } from 'react'
import { catalog, type DataFieldRow, type DatasetRow, type FieldFilter } from '@/api/catalog'
import type { Scope } from '@/api/types'
import { cn } from '@/lib/cn'
import { fmt } from '@/lib/format'
import { useDebounced } from '@/lib/use-debounced'
import { DatasetChips, useDatasetTree } from '@/screens/data/dataset-chips'
import { DatasetPicker } from '@/screens/data/dataset-picker'
import { describeFilter, type FieldFilterState } from '@/screens/data/state'
import {
  Badge,
  Button,
  Checkbox,
  Chips,
  Empty,
  ErrorNotice,
  Field,
  Fieldset,
  Input,
  Panel,
  Segmented,
  Skeleton,
  Textarea,
} from '@/ui/kit'
import { Sheet } from '@/ui/overlay'
import type { FieldsVariable, VariableDef, VariableInfo } from './api'

type FieldType = 'MATRIX' | 'VECTOR'
const TYPES: FieldType[] = ['MATRIX', 'VECTOR']
const TYPE_LABELS: Record<FieldType, string> = { MATRIX: 'Matrix', VECTOR: 'Vector' }

/** Comments blanked as BRAIN reads them, so a `$name` in one is not a variable. */
const COMMENTS = /\/\*[\s\S]*?\*\/|(?:\/\/|#)[^\n]*/g

/** Every `$name` the template writes, in the order first written. */
export function namesIn(text: string): string[] {
  const found = text.replace(COMMENTS, ' ').matchAll(/\$([A-Za-z_]\w*)/g)
  return [...new Set([...found].map((m) => m[1] ?? ''))].filter(Boolean)
}

/**
 * What a name means before anyone defines it: a preset's values, or the fields of the dataset,
 * subcategory or category it is named after. `$earnings4_b` reads like `$earnings4`, so a
 * second field from one dataset is a suffix away.
 */
export function defaultOf(
  name: string,
  presets: Record<string, string>,
  datasets: DatasetRow[],
): VariableDef | null {
  for (const stem of new Set([name, name.replace(/_[a-z0-9]+$/i, '')])) {
    const preset = presets[stem]
    if (preset) return { kind: 'values', values: preset }
    if (datasets.some((d) => d.dataset_id === stem)) return { kind: 'fields', dataset_ids: [stem] }
    for (const level of ['subcategory_id', 'category_id'] as const) {
      const ids = datasets
        .filter((d) => d[level]?.replaceAll('-', '_') === stem)
        .map((d) => d.dataset_id)
      if (ids.length) return { kind: 'fields', dataset_ids: ids }
    }
  }
  return null
}

/** What each `$name` in `text` stands for: given here, else its default; undefined ones left out. */
export function resolve(
  text: string,
  given: Record<string, VariableDef>,
  presets: Record<string, string>,
  datasets: DatasetRow[],
): Record<string, VariableDef> {
  return Object.fromEntries(
    namesIn(text).flatMap((name) => {
      const def = given[name] ?? defaultOf(name, presets, datasets)
      return def ? [[name, def] as const] : []
    }),
  )
}

/** The rows: each name, with its definition and what the preview says about it. */
export function VariablesPanel({
  names,
  defs,
  given,
  infos,
  scope,
  presets,
  vector,
  pending,
  onDefine,
  onMoreFilters,
}: {
  names: string[]
  /** Every name's definition as it stands: given here, or its default. */
  defs: Record<string, VariableDef | null>
  /** The names defined here rather than by default. */
  given: ReadonlySet<string>
  infos: Map<string, VariableInfo>
  /** The market, as one of its universes, for the catalog's own queries. */
  scope: Scope | null
  presets: Record<string, string>
  vector: string[]
  pending: boolean
  onDefine: (name: string, variable: VariableDef | null) => void
  onMoreFilters: (name: string, variable: FieldsVariable) => void
}) {
  const [editing, setEditing] = useState<string | null>(null)
  const { tree, nameOf, ready } = useDatasetTree(scope)

  return (
    <Panel
      title="Variables"
      description="Each $name the template writes. Every Alpha takes one value for each."
    >
      {names.length === 0 ? (
        <Empty title="No variables yet">
          Write $name in the template wherever the search should choose: $earnings4 takes a field
          from that dataset, $slow_lookback one of 63, 126 or 252, and any other name is yours to
          define.
        </Empty>
      ) : (
        <ul className="flex flex-col divide-y divide-hairline-subtle">
          {names.map((name) => {
            const def = defs[name] ?? null
            const info = infos.get(name)
            return (
              <li key={name} className="flex flex-col gap-2 py-3 first:pt-0 last:pb-0">
                <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
                  <div className="flex min-w-0 flex-1 flex-col gap-1.5">
                    <div className="flex flex-wrap items-center gap-2">
                      <code className="num text-body font-medium text-ink">${name}</code>
                      {def && (
                        <Badge tone="outline">{def.kind === 'fields' ? 'Fields' : 'Values'}</Badge>
                      )}
                      {info?.operator && <Badge tone="outline">Operator</Badge>}
                      {def && !given.has(name) && (
                        <span className="text-body-compact text-ink-subtle">
                          {def.kind === 'values' ? 'Preset' : 'Named after its datasets'}
                        </span>
                      )}
                    </div>
                    <Summary
                      def={def}
                      info={info}
                      scope={scope}
                      tree={tree}
                      nameOf={nameOf}
                      ready={ready}
                    />
                  </div>
                  <div className="flex items-center gap-3">
                    <Count def={def} info={info} pending={pending} />
                    <Button size="sm" onClick={() => setEditing(name)}>
                      <PencilIcon />
                      {def ? 'Edit' : 'Define'}
                    </Button>
                  </div>
                </div>
                {info?.problems.map((problem) => (
                  <p key={problem} className="text-body-compact text-status-danger">
                    {problem}
                  </p>
                ))}
              </li>
            )
          })}
        </ul>
      )}
      {editing !== null && (
        <VariableEditor
          key={editing}
          name={editing}
          initial={defs[editing] ?? null}
          given={given.has(editing)}
          info={infos.get(editing)}
          scope={scope}
          presets={presets}
          vector={vector}
          onClose={() => setEditing(null)}
          onApply={(variable) => {
            onDefine(editing, variable)
            setEditing(null)
          }}
          onMoreFilters={(variable) => onMoreFilters(editing, variable)}
        />
      )}
    </Panel>
  )
}

function Summary({
  def,
  info,
  scope,
  tree,
  nameOf,
  ready,
}: {
  def: VariableDef | null
  info: VariableInfo | undefined
  scope: Scope | null
  tree: ReturnType<typeof useDatasetTree>['tree']
  nameOf: (id: string) => string
  ready: boolean
}) {
  if (!def)
    return (
      <span className="text-body-compact text-ink-subtle">
        Not defined: choose its fields, or type its values.
      </span>
    )
  if (def.kind === 'values')
    return <code className="num text-body-compact break-words text-ink-muted">{def.values}</code>
  const types = info?.fieldTypes ?? []
  const narrowed = describeFilter({ ...def.filter, field_types: [] }, scope?.region ?? '')
  return (
    <div className="flex flex-col gap-1.5">
      <DatasetChips tree={tree} value={def.dataset_ids} nameOf={nameOf} ready={ready} />
      <span className="text-body-compact text-ink-subtle">
        {[
          types.map((t) => TYPE_LABELS[t as FieldType] ?? t).join(' and '),
          types.includes('MATRIX') && types.includes('VECTOR')
            ? `vector fields through ${(def.vector_operators ?? []).join(', ') || '…'}`
            : null,
          ...narrowed,
        ]
          .filter(Boolean)
          .join(' · ')}
      </span>
    </div>
  )
}

function Count({
  def,
  info,
  pending,
}: {
  def: VariableDef | null
  info: VariableInfo | undefined
  pending: boolean
}) {
  if (!def || !info || info.kind !== def.kind) return null
  const text =
    def.kind === 'values'
      ? `${fmt.int(info.values)} value${info.values === 1 ? '' : 's'}`
      : info.fields
        ? `${fmt.int(info.fields.total)} field${info.fields.total === 1 ? '' : 's'}`
        : null
  if (!text) return null
  const split =
    info.fields && info.fields.matrix > 0 && info.fields.vector > 0
      ? `${fmt.int(info.fields.matrix)} matrix, ${fmt.int(info.fields.vector)} vector`
      : undefined
  return (
    <span
      title={split}
      className={cn(
        'num text-body-compact whitespace-nowrap',
        pending ? 'text-ink-subtle' : 'text-ink',
      )}
    >
      {text}
    </span>
  )
}

/** Defines one variable: its datasets and filters, or its values. */
function VariableEditor({
  name,
  initial,
  given,
  info,
  scope,
  presets,
  vector,
  onClose,
  onApply,
  onMoreFilters,
}: {
  name: string
  initial: VariableDef | null
  given: boolean
  info: VariableInfo | undefined
  scope: Scope | null
  presets: Record<string, string>
  vector: string[]
  onClose: () => void
  /** `null` drops what was given here, so the name starts from its default again. */
  onApply: (variable: VariableDef | null) => void
  onMoreFilters: (variable: FieldsVariable) => void
}) {
  const [kind, setKind] = useState<VariableDef['kind']>(
    initial?.kind ?? (info?.operator ? 'values' : 'fields'),
  )
  const [datasetIds, setDatasetIds] = useState<string[]>(
    initial?.kind === 'fields' ? initial.dataset_ids : [],
  )
  const [filter, setFilter] = useState<FieldFilterState>(
    initial?.kind === 'fields' ? (initial.filter ?? {}) : {},
  )
  const [wraps, setWraps] = useState<string[]>(
    initial?.kind === 'fields' ? (initial.vector_operators ?? []) : [],
  )
  const [values, setValues] = useState(initial?.kind === 'values' ? initial.values : '')
  // The dataset of each field picked here. A variable searches only its datasets, so a picked
  // field brings its own along, and unticking that dataset drops the field again.
  const [homes, setHomes] = useState<Record<string, string>>({})
  const picked = filter.field_ids ?? []
  const brought = picked.flatMap((id) => homes[id] ?? [])
  const datasets = [...new Set([...datasetIds, ...brought])]
  const pickFields = (ids: string[], field?: DataFieldRow) => {
    const home = field?.dataset_id
    if (field && home) setHomes((h) => ({ ...h, [field.field_id]: home }))
    setFilter(({ field_ids: _, ...f }) => (ids.length ? { ...f, field_ids: ids } : f))
  }
  const tickDatasets = (ids: string[]) => {
    setDatasetIds(ids.filter((id) => datasetIds.includes(id) || !brought.includes(id)))
    pickFields(picked.filter((id) => !homes[id] || ids.includes(homes[id])))
  }

  // Types not chosen here follow where the template writes the variable, as the preview read it.
  const own = (filter.field_types ?? []).filter((t): t is FieldType =>
    TYPES.includes(t as FieldType),
  )
  // Matrix until the preview has read them: an undefined variable comes back with none.
  const types = (
    own.length ? own : info?.fieldTypes.length ? info.fieldTypes : ['MATRIX']
  ) as FieldType[]
  const both = types.includes('MATRIX') && types.includes('VECTOR')
  const fields: FieldsVariable = {
    kind: 'fields',
    dataset_ids: datasets,
    filter: Object.keys(filter).length ? filter : null,
    vector_operators: both ? wraps : [],
  }
  const draft: VariableDef = kind === 'fields' ? fields : { kind: 'values', values }

  const setTypes = (next: string[]) => {
    const chosen = TYPES.filter((t) => next.includes(t))
    if (chosen.length === 0) return
    setFilter((f) => ({ ...f, field_types: chosen }))
    // Both kinds of field need something to reduce the vector ones with: vec_avg, unless
    // something else was already chosen.
    if (chosen.length === 2 && wraps.length === 0 && vector.includes('vec_avg'))
      setWraps(['vec_avg'])
  }

  return (
    <Sheet
      open
      onOpenChange={(open) => !open && onClose()}
      title={<code className="num">${name}</code>}
      description={
        info?.operator
          ? 'Written as an operator, so its values are operator names.'
          : 'Every Alpha takes one of its values.'
      }
      footer={
        <>
          {given && (
            <Button variant="ghost" className="mr-auto" onClick={() => onApply(null)}>
              <RotateCcwIcon />
              Use Default
            </Button>
          )}
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" onClick={() => onApply(draft)}>
            Apply
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <Segmented
          label="Kind"
          items={[
            { value: 'fields' as const, label: 'Fields' },
            { value: 'values' as const, label: 'Values' },
          ]}
          value={kind}
          onChange={setKind}
        />
        {kind === 'values' ? (
          <Field
            label="Values"
            hint={
              'Separated by commas: numbers, groups, operator names, or whole expressions ' +
              'such as bucket(rank(cap), range="0, 1, 0.1").'
            }
          >
            <Textarea
              rows={4}
              spellCheck={false}
              className="num"
              value={values}
              onChange={(e) => setValues(e.target.value)}
            />
            {presets[name] !== undefined && values !== presets[name] && (
              <Button
                size="sm"
                variant="ghost"
                className="self-start"
                onClick={() => setValues(presets[name] ?? '')}
              >
                <RotateCcwIcon />
                Preset Values
              </Button>
            )}
          </Field>
        ) : scope === null ? (
          <Empty title="This market is not downloaded" icon={<DatabaseIcon />}>
            Download it in BRAIN › Sync to choose fields from it.
          </Empty>
        ) : (
          <FieldsChoice
            scope={scope}
            datasetIds={datasets}
            setDatasetIds={tickDatasets}
            filter={filter}
            setFilter={setFilter}
            types={types}
            setTypes={setTypes}
            vector={vector}
            wraps={wraps}
            setWraps={setWraps}
            picked={picked}
            onPick={pickFields}
            onMoreFilters={() => onMoreFilters(fields)}
          />
        )}
      </div>
    </Sheet>
  )
}

function FieldsChoice({
  scope,
  datasetIds,
  setDatasetIds,
  filter,
  setFilter,
  types,
  setTypes,
  vector,
  wraps,
  setWraps,
  picked,
  onPick,
  onMoreFilters,
}: {
  scope: Scope
  datasetIds: string[]
  setDatasetIds: (ids: string[]) => void
  filter: FieldFilterState
  setFilter: (change: (f: FieldFilterState) => FieldFilterState) => void
  types: FieldType[]
  setTypes: (types: string[]) => void
  vector: string[]
  wraps: string[]
  setWraps: (ops: string[]) => void
  picked: string[]
  onPick: (ids: string[], field?: DataFieldRow) => void
  onMoreFilters: () => void
}) {
  return (
    <DatasetPicker
      scope={scope}
      datasetIds={datasetIds}
      setDatasetIds={setDatasetIds}
      filter={filter}
      setFilter={setFilter}
      types={types}
      onMoreFilters={onMoreFilters}
    >
      {(byType) => (
        <>
          <Fieldset legend="Field Type">
            <Chips
              label="Field Type"
              value={types}
              onChange={setTypes}
              items={TYPES.map((t) => ({
                value: t,
                label: (
                  <span>
                    {TYPE_LABELS[t]}{' '}
                    <span className="num text-ink-subtle">
                      {fmt.int(byType ? (byType.get(t) ?? 0) : null)}
                    </span>
                  </span>
                ),
              }))}
            />
          </Fieldset>
          {types.includes('MATRIX') && types.includes('VECTOR') && (
            <Fieldset
              legend="Vector Operators"
              hint="A vector field is reduced with one of these; matrix fields are used as they are."
            >
              <Chips
                label="Vector Operators"
                value={wraps}
                onChange={setWraps}
                items={vector.map((op) => ({ value: op, label: op }))}
              />
            </Fieldset>
          )}
          <FieldPicker
            scope={scope}
            filter={filter}
            types={types}
            picked={picked}
            onChange={onPick}
          />
        </>
      )}
    </DatasetPicker>
  )
}

/**
 * Exact fields, found by searching every dataset of the market. Matches are only fields the
 * variable can take: of its types, and under its other filters.
 */
function FieldPicker({
  scope,
  filter,
  types,
  picked,
  onChange,
}: {
  scope: Scope
  filter: FieldFilterState
  types: FieldType[]
  picked: string[]
  /** With the field just ticked, so its dataset can come along. */
  onChange: (ids: string[], ticked?: DataFieldRow) => void
}) {
  const [search, setSearch] = useState('')
  const typed = useDebounced(search.trim(), 250)
  const body: FieldFilter = {
    ...filter,
    search: typed,
    search_mode: 'smart',
    dataset_ids: [],
    field_ids: [],
    field_types: types,
    // The most used first: ranked by relevance, `close` falls behind every id that repeats it.
    sort_by: 'alpha_count',
    limit: 50,
  }
  const found = useQuery({
    queryKey: ['catalog', 'fields', scope, body],
    queryFn: ({ signal }) => catalog.fields(scope, body, signal),
    enabled: typed !== '',
    placeholderData: keepPreviousData,
  })
  const rows = found.data?.results ?? []
  const total = found.data?.total ?? 0

  return (
    <Fieldset
      legend="Exact Fields"
      hint="Search every dataset in this market and tick fields: with any ticked, the variable takes only those."
    >
      {picked.length > 0 && (
        <Chips
          label="Exact Fields"
          value={picked}
          onChange={(kept) => onChange(kept)}
          items={picked.map((id) => ({
            value: id,
            title: 'Remove this field',
            label: (
              <>
                <span className="num">{id}</span> <span className="text-ink-subtle">×</span>
              </>
            ),
          }))}
        />
      )}
      <Input
        placeholder="Search Field ID or Description"
        aria-label="Search fields to pick"
        value={search}
        onChange={(e) => setSearch(e.target.value)}
      />
      {typed !== '' &&
        (found.isError ? (
          <ErrorNotice error={found.error} title="Could not search the fields" />
        ) : (
          <div
            role="group"
            aria-label="Matching fields"
            className="flex max-h-60 flex-col overflow-y-auto rounded-md border border-hairline py-1"
          >
            {found.isPending ? (
              <Skeleton className="mx-3 my-2 h-12" />
            ) : rows.length === 0 ? (
              <p className="px-3 py-2 text-body-compact text-ink-subtle">No field matches.</p>
            ) : (
              rows.map((row) => (
                <Checkbox
                  key={row.field_id}
                  className="flex px-3 py-1.5 hover:bg-surface-2"
                  checked={picked.includes(row.field_id)}
                  onChange={(on) =>
                    on
                      ? onChange([...picked, row.field_id], row)
                      : onChange(picked.filter((id) => id !== row.field_id))
                  }
                  label={
                    <>
                      <span className="num truncate text-body-compact">{row.field_id}</span>
                      <span className="truncate text-caption text-ink-subtle">
                        {[row.dataset_id, row.description].filter(Boolean).join(' · ')}
                      </span>
                    </>
                  }
                />
              ))
            )}
            {total > rows.length && (
              <p className="px-3 py-1.5 text-caption text-ink-subtle">
                The first {fmt.int(rows.length)} of {fmt.int(total)}: type more to narrow them.
              </p>
            )}
          </div>
        ))}
    </Fieldset>
  )
}
