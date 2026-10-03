/**
 * Ticking single fields in the Fields table, and ranking them for a lab. The ticks outlive
 * paging and filtering, so a researcher can gather fields over several searches; the order
 * they reach a lab in is the table's order at the moment they are sent. Exclusion filters keep
 * fields out of the table always, and out of the selection without unticking them, so undoing
 * one restores them.
 */

import { keepPreviousData, useMutation, useQuery } from '@tanstack/react-query'
import { useNavigate } from '@tanstack/react-router'
import { toast } from 'sonner'
import { create } from 'zustand'
import { createJSONStorage, persist } from 'zustand/middleware'
import { catalog, type DataFieldRow, type FieldFilter, type FieldSortKey } from '@/api/catalog'
import type { Scope } from '@/api/types'
import type { Sort } from '@/ui/table'
import {
  type DatasetPickExtra,
  type FieldPick,
  type PickedField,
  type PickFrom,
  useDatasetPick,
} from './dataset-pick'
import { type FieldFilterState, labFilter, RELEVANCE, useFieldFilter } from './state'

/**
 * The labs that take single fields. Advanced Template Research does not: its variables are
 * datasets under a filter, so ticks made while picking for it are left out and the datasets
 * go with the filter that was showing.
 */
export const TAKES_FIELDS: ReadonlySet<PickFrom> = new Set<PickFrom>([
  '/labs/search',
  '/labs/template/basic',
  '/labs/power-pool',
  '/labs/region-agnostic',
])

/** Matches `labs.launch.MAX_PICKED_FIELDS`: past this, a lab wants whole datasets. */
export const MAX_PICKED_FIELDS = 500

interface FieldSelection {
  /** The market the ticks belong to; fields are per region and delay. */
  scope: Scope | null
  fields: PickedField[]
  /** Replaces the ticks, as a lab does when it opens the table with its own. */
  load: (scope: Scope, fields: PickedField[]) => void
  toggle: (
    scope: Scope,
    rows: Pick<DataFieldRow, 'field_id' | 'dataset_id' | 'field_type'>[],
    on: boolean,
  ) => void
  clear: () => void
}

const sameMarket = (a: Scope | null, b: Scope) =>
  a !== null && a.region === b.region && a.delay === b.delay

export const useFieldSelection = create<FieldSelection>()(
  persist(
    (set, get) => ({
      scope: null,
      fields: [],
      load: (scope, fields) => set({ scope, fields }),
      toggle: (scope, rows, on) => {
        // Ticks from another market name fields this one may not have: start over.
        const kept = sameMarket(get().scope, scope) ? get().fields : []
        const ids = new Set(rows.map((r) => r.field_id))
        const rest = kept.filter((f) => !ids.has(f.id))
        const added = rows.map((r) => ({
          id: r.field_id,
          dataset: r.dataset_id ?? '',
          type: r.field_type,
        }))
        set({ scope, fields: on ? [...rest, ...added].slice(0, MAX_PICKED_FIELDS) : rest })
      },
      clear: () => set({ scope: null, fields: [] }),
    }),
    { name: 'alpha-harness-field-selection', storage: createJSONStorage(() => sessionStorage) },
  ),
)

/** The ticks for `scope`'s market, and nothing when they belong to another. */
export function useTicked(scope: Scope): PickedField[] {
  const selection = useFieldSelection()
  return sameMarket(selection.scope, scope) ? selection.fields : NONE
}
const NONE: PickedField[] = []

// ── Exclusions ──────────────────────────────────────────────────────────────────────────

/** The filter keys that take fields out of a selection, and out of the table with them. */
export const EXCLUSION_KEYS = [
  'exclude_dataset_ids',
  'exclude_date_min',
  'exclude_date_max',
  'exclude_keywords',
] as const satisfies (keyof FieldFilterState)[]

type Exclusions = Pick<FieldFilterState, (typeof EXCLUSION_KEYS)[number]>

export const exclusionsOf = (filter: FieldFilterState): Exclusions =>
  Object.fromEntries(EXCLUSION_KEYS.map((k) => [k, filter[k]])) as Exclusions

const isOn = (v: unknown) => v != null && v !== '' && !(Array.isArray(v) && v.length === 0)

/** How many exclusion filters are set. */
export const exclusionCount = (filter: FieldFilterState) =>
  EXCLUSION_KEYS.filter((k) => isOn(filter[k])).length

/** Why an exclusion filter takes `row` out, in a few words; null when none does. */
export function excludedBy(row: DataFieldRow, filter: FieldFilterState): string | null {
  if (row.field_type === 'GROUP') return 'Grouping field: every lab has it'
  if (row.dataset_id && filter.exclude_dataset_ids?.includes(row.dataset_id))
    return `Dataset ${row.dataset_id}`
  const day = row.date_created?.slice(0, 10)
  const from = filter.exclude_date_min
  const to = filter.exclude_date_max
  if (day && (from || to) && (!from || day >= from) && (!to || day <= to)) return `Added ${day}`
  const text = row.description?.toLowerCase() ?? ''
  const word = filter.exclude_keywords?.find(
    (w) => w.trim() && text.includes(w.trim().toLowerCase()),
  )
  if (word) return `Description has “${word.trim()}”`
  return null
}

// ── Ranking ─────────────────────────────────────────────────────────────────────────────

/** The columns a table can be sorted by, as a sentence names them. */
const RANKS: Record<string, { name: string; desc: string; asc: string }> = {
  alpha_count: { name: 'Alphas', desc: 'most first', asc: 'fewest first' },
  user_count: { name: 'Users', desc: 'most first', asc: 'fewest first' },
  coverage: { name: 'Instrument Coverage', desc: 'highest first', asc: 'lowest first' },
  date_coverage: { name: 'Date Coverage', desc: 'highest first', asc: 'lowest first' },
  pyramid_multiplier: {
    name: 'Pyramid Theme Multiplier',
    desc: 'highest first',
    asc: 'lowest first',
  },
  date_created: { name: 'Date added', desc: 'newest first', asc: 'oldest first' },
  field_id: { name: 'Field id', desc: 'Z to A', asc: 'A to Z' },
  dataset_id: { name: 'Dataset', desc: 'Z to A', asc: 'A to Z' },
  category_id: { name: 'Category', desc: 'Z to A', asc: 'A to Z' },
  field_type: { name: 'Type', desc: 'Z to A', asc: 'A to Z' },
}

/** "Alphas, most first", or "best match to “earnings”" for a ranked search. */
export function rankLabel(sort: Sort, search: string | null | undefined): string {
  if (sort.key === RELEVANCE) return search ? `best match to “${search}”` : 'best match'
  const rank = RANKS[sort.key] ?? { name: sort.key, desc: 'highest first', asc: 'lowest first' }
  return `${rank.name}, ${sort.desc ? rank.desc : rank.asc}`
}

/**
 * The ticks the exclusion filters leave, in the order the table is sorted by now, whichever
 * page or filter each was ticked under. The backend sorts them, so the order is the table's
 * own, ties and all. Grouping fields never go: every lab has them already.
 */
export async function rankFields(
  scope: Scope,
  fields: PickedField[],
  sort: Sort,
  filter: FieldFilterState,
): Promise<FieldPick> {
  const rankBy = rankLabel(sort, filter.search)
  const candidates = fields.filter((f) => f.type !== 'GROUP')
  if (candidates.length === 0) return { fields: [], rankBy }
  const base: FieldFilter = {
    ...exclusionsOf(filter),
    field_ids: candidates.map((f) => f.id),
    field_types: ['MATRIX', 'VECTOR'],
    sort_by: sort.key as FieldSortKey,
    sort_desc: sort.desc,
    limit: candidates.length,
    offset: 0,
  }
  const kept = await catalog.fields(scope, { ...base, sort_by: 'field_id' })
  const keptIds = new Set(kept.results.map((r) => r.field_id))
  // Only a ranked search has a score to order by; any other sort ignores the search.
  const page =
    sort.key === RELEVANCE
      ? await catalog.fields(scope, {
          ...base,
          search: filter.search ?? null,
          search_mode: filter.search_mode ?? 'smart',
        })
      : await catalog.fields(scope, base)
  const byId = new Map(candidates.map((f) => [f.id, f]))
  const ranked = page.results.flatMap((r) => byId.get(r.field_id) ?? [])
  // A tick the search no longer matches still goes, after every field it does rank.
  const placed = new Set(ranked.map((f) => f.id))
  const rest = candidates.filter((f) => keptIds.has(f.id) && !placed.has(f.id))
  return { fields: [...ranked, ...rest], rankBy }
}

/** The datasets the fields come from, in the order they first appear. */
export const datasetsOf = (fields: PickedField[]) => [...new Set(fields.map((f) => f.dataset))]

export interface SelectionSplit {
  /** What a lab would get: the ticks the exclusions leave, ranked. */
  selected: DataFieldRow[]
  /** Ticks an exclusion filter takes out, with which one. */
  excluded: { row: DataFieldRow; reason: string }[]
  rankBy: string
  loading: boolean
  error: unknown
}

/** The selection as a lab would receive it, and what the exclusion filters took out of it. */
export function useSelectionSplit(scope: Scope): SelectionSplit {
  const ticked = useTicked(scope)
  const { sort, filter } = useFieldFilter()
  const ids = ticked.map((f) => f.id)
  const on = ids.length > 0
  const rows = useQuery({
    queryKey: ['catalog', 'selection', scope, ids],
    queryFn: () => catalog.fields(scope, { field_ids: ids, limit: ids.length, offset: 0 }),
    enabled: on,
    placeholderData: keepPreviousData,
  })
  const exclusions = exclusionsOf(filter)
  const search = sort.key === RELEVANCE ? filter.search : null
  const ranked = useQuery({
    queryKey: ['catalog', 'selection', scope, ids, 'ranked', sort, search, exclusions],
    queryFn: () => rankFields(scope, ticked, sort, filter),
    enabled: on,
    placeholderData: keepPreviousData,
  })
  const byId = new Map((rows.data?.results ?? []).map((r) => [r.field_id, r]))
  const selected = on ? (ranked.data?.fields ?? []).flatMap((f) => byId.get(f.id) ?? []) : []
  const kept = new Set(selected.map((r) => r.field_id))
  const excluded = on
    ? (rows.data?.results ?? [])
        .filter((r) => !kept.has(r.field_id))
        .map((row) => ({ row, reason: excludedBy(row, filter) ?? 'Excluded' }))
    : []
  return {
    selected,
    excluded,
    rankBy: rankLabel(sort, filter.search),
    loading: on && (rows.isPending || ranked.isPending),
    error: rows.error ?? ranked.error,
  }
}

/**
 * Hands the selection to a lab: what the exclusions leave, ranked by the table's sort, with the
 * datasets it comes from. `finish` ends a pick the lab started; otherwise the pick is made here
 * and sent. With nothing selected, a pick keeps the datasets ticked in More filters and tells
 * the lab to use them whole.
 */
export function useHandOver() {
  const navigate = useNavigate()
  return useMutation({
    mutationFn: async ({ scope, to, finish }: { scope: Scope; to: PickFrom; finish: boolean }) => {
      const selection = useFieldSelection.getState()
      const ticked =
        TAKES_FIELDS.has(to) && sameMarket(selection.scope, scope) ? selection.fields : []
      const { sort, filter } = useFieldFilter.getState()
      const pick: FieldPick = ticked.length
        ? await rankFields(scope, ticked, sort, filter)
        : { fields: [], rankBy: null }
      const left = ticked.length - pick.fields.length
      if (left > 0)
        toast.info(`${left} selected ${left === 1 ? 'field was' : 'fields were'} left out`, {
          description: 'Excluded by the exclusion filters, or grouping fields every lab has.',
        })
      if (ticked.length > 0 && pick.fields.length === 0)
        throw new Error('Every selected field is excluded. Loosen the exclusion filters.')
      const datasets = useDatasetPick.getState()
      // Ticked fields are exactly what was wanted; with none, the datasets go under the
      // filter that was showing, so the lab uses only the fields it shows.
      const extra: DatasetPickExtra = {
        ...pick,
        filter: pick.fields.length ? null : labFilter(filter),
      }
      if (finish) {
        if (pick.fields.length) useDatasetPick.setState({ ids: datasetsOf(pick.fields) })
        datasets.finish(extra)
      } else {
        datasets.hand(scope, datasetsOf(pick.fields), to, extra)
      }
      selection.clear()
      await navigate({ to })
    },
  })
}
