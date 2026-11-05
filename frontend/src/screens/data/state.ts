/** Non-component pieces the Data Explorer tabs share: the Fields filter store and helpers. */

import { create } from 'zustand'
import type { FieldFilter, FieldSortKey } from '@/api/catalog'
import type { Scope } from '@/api/types'
import { cn } from '@/lib/cn'
import { DASH, fmt, isNum } from '@/lib/format'
import { regionLabel } from '@/lib/scope'
import { type PickFrom, useDatasetPick, useFieldPick } from '@/screens/data/dataset-pick'
import { STATUS } from '@/ui/kit'
import type { Sort } from '@/ui/table'

/** The kit's STATUS box, one size down for the dense Data Explorer rows. */
export const STAT = cn(STATUS, 'inline-flex h-6 px-2')

export type FieldFilterState = Omit<FieldFilter, 'sort_by' | 'sort_desc' | 'limit' | 'offset'>

interface FieldFilterStore {
  filter: FieldFilterState
  sort: Sort
  /**
   * Whether the reader picked {@link sort} themselves. Recorded rather than inferred: the
   * default *is* Alphas descending, so clicking that column is indistinguishable from never
   * having clicked at all.
   */
  chosen: boolean
  offset: number
  set: (change: Partial<FieldFilterState>) => void
  replace: (filter: FieldFilterState) => void
  setSort: (sort: Sort) => void
  /** Order by how well each row answers the search — the way back from a column. */
  rank: () => void
  page: (offset: number) => void
}

/** What a search is sorted by until the reader picks a column, and what it goes back to. */
export const RELEVANCE = 'relevance'
const DEFAULT_SORT: Sort = { key: 'alpha_count', desc: true }

/**
 * The sort a change to the search box leaves behind.
 *
 * Relevance exists only while a smart search is running, so it has to give way when the box
 * empties or the mode turns exact. A column the reader picked themselves outlives all of
 * that: switching modes to compare two readings of the same search must not silently
 * re-sort the table under them.
 */
function sortAfter(previous: FieldFilterStore, filter: FieldFilterState): Sort {
  const rankable = !!filter.search && (filter.search_mode ?? 'smart') === 'smart'
  const { sort, chosen } = previous
  if (sort.key === RELEVANCE) return rankable ? sort : DEFAULT_SORT
  return rankable && !chosen ? { key: RELEVANCE, desc: true } : sort
}

/** Lives outside the Fields tab so the filter survives leaving the Data Explorer and coming back. */
export const useFieldFilter = create<FieldFilterStore>()((set) => ({
  filter: {},
  sort: DEFAULT_SORT,
  chosen: false,
  offset: 0,
  set: (change) =>
    set((s) => {
      const filter = { ...s.filter, ...change }
      // Emptying the box ends the search the column was picked for, so the next one is free
      // to rank itself again. Without this, one column click silences ranking for the session.
      const cleared = 'search' in change && !change.search && !!s.filter.search
      const chosen = cleared ? false : s.chosen
      return { filter, chosen, sort: sortAfter({ ...s, chosen }, filter), offset: 0 }
    }),
  // Clearing the filters clears how they were ordered too, so the next search can rank again.
  replace: (filter) => set({ filter, sort: DEFAULT_SORT, chosen: false, offset: 0 }),
  setSort: (sort) =>
    set({
      sort: { key: sort.key as FieldSortKey, desc: sort.desc },
      chosen: true,
      offset: 0,
    }),
  // Not `chosen`: asking for the best match is handing the ordering back to the search, so a
  // later search ranks itself again instead of being held to this one.
  rank: () => set({ sort: { key: RELEVANCE, desc: true }, chosen: false, offset: 0 }),
  page: (offset) => set({ offset }),
}))

/** The reader's own filter while a lab picks under its own, given back when the pick ends. */
let browsing: Pick<FieldFilterStore, 'filter' | 'sort' | 'chosen'> | null = null

/** Opens a dataset pick for a lab, the Explorer showing the fields `filter` leaves. */
export function startDatasetPick(
  scope: Scope,
  ids: string[],
  from: PickFrom,
  filter: FieldFilterState | null,
) {
  const { filter: own, sort, chosen } = useFieldFilter.getState()
  browsing ??= { filter: own, sort, chosen }
  // The lab's own fields come back as ticks, not as a filter that would hide the rest.
  const { field_ids: fields = [], ...rest } = filter ?? {}
  useFieldPick.setState({ fields: Object.fromEntries(fields.map((id) => [id, ''])) })
  useFieldFilter.getState().replace(rest)
  useDatasetPick.getState().start(scope, ids, from)
}

/** Done hands the lab its datasets and the filter they were chosen under; either way the
 *  reader's own filter comes back. */
export function endDatasetPick(done: boolean) {
  const pick = useDatasetPick.getState()
  const { fields } = useFieldPick.getState()
  if (done) {
    const ticked = Object.keys(fields)
    useDatasetPick.setState({
      ids: [...new Set([...pick.ids, ...Object.values(fields).filter(Boolean)])],
    })
    pick.finish(labFilter({ ...useFieldFilter.getState().filter, field_ids: ticked }))
  } else pick.cancel()
  useFieldPick.setState({ fields: {} })
  useFieldFilter.setState({
    ...(browsing ?? { filter: {}, sort: DEFAULT_SORT, chosen: false }),
    offset: 0,
  })
  browsing = null
}

const NONE: string[] = []

/**
 * The datasets the Fields tab filters on, and how to change them: while a lab picks datasets,
 * that pick (kept across a reload); otherwise the Fields filter's own.
 */
export function useDatasetChoice(): [string[], (ids: string[]) => void] {
  const picking = useDatasetPick((s) => s.active)
  const picked = useDatasetPick((s) => s.ids)
  const filtered = useFieldFilter((s) => s.filter.dataset_ids ?? NONE)
  if (picking) {
    return [
      picked,
      (ids) => {
        useDatasetPick.setState({ ids })
        useFieldFilter.getState().page(0)
      },
    ]
  }
  return [filtered, (ids) => useFieldFilter.getState().set({ dataset_ids: ids })]
}

export const isActive = (v: unknown) =>
  v != null && v !== '' && v !== false && !(Array.isArray(v) && v.length === 0)

/**
 * The filter a lab takes with its datasets: what is set, bar the datasets themselves, which
 * are the pick. `null` when nothing narrows the fields, so a lab can tell "no filter" at once.
 */
export function labFilter(filter: FieldFilterState): FieldFilterState | null {
  const kept = Object.fromEntries(
    Object.entries(filter).filter(([key, value]) => key !== 'dataset_ids' && isActive(value)),
  ) as FieldFilterState
  // The search mode only means something beside a search.
  if (!kept.search) delete kept.search_mode
  return Object.keys(kept).length > 0 ? kept : null
}

/** A filter in words, one entry per thing it narrows on, for where it is applied away from here. */
export function describeFilter(filter: FieldFilterState, region: string): string[] {
  const range = <T>(
    name: string,
    min: T | null | undefined,
    max: T | null | undefined,
    show: (v: T) => string,
  ) => {
    const lo = min == null ? null : show(min)
    const hi = max == null ? null : show(max)
    if (lo && hi) return `${name} ${lo}\u2013${hi}`
    if (lo) return `${name} \u2265 ${lo}`
    return hi ? `${name} \u2264 ${hi}` : null
  }
  const pct = (v: number) => fmt.pct(v, 0)
  return [
    filter.search && `Search \u201c${filter.search}\u201d`,
    filter.region_agnostic && 'Region Agnostic',
    filter.region_exclusive && `${regionLabel(region)} Exclusive`,
    filter.category_ids?.length &&
      `${fmt.int(filter.category_ids.length)} ${filter.category_ids.length === 1 ? 'Category' : 'Categories'}`,
    filter.field_types?.length && filter.field_types.join(', '),
    filter.field_ids?.length &&
      `${fmt.int(filter.field_ids.length)} ${filter.field_ids.length === 1 ? 'Field' : 'Fields'}`,
    range('Instrument Coverage', filter.coverage_min, filter.coverage_max, pct),
    range('Date Coverage', filter.date_coverage_min, filter.date_coverage_max, pct),
    range('Alphas', filter.alpha_count_min, filter.alpha_count_max, fmt.int),
    range('Users', filter.user_count_min, filter.user_count_max, fmt.int),
    range(
      'Pyramid Theme Multiplier',
      filter.pyramid_multiplier_min,
      filter.pyramid_multiplier_max,
      multiplier,
    ),
    range('Date Added', filter.date_created_from, filter.date_created_to, fmt.month),
  ].filter((part): part is string => typeof part === 'string' && part.length > 0)
}

export const sameScope = (a: Scope, b: Scope) =>
  a.region === b.region &&
  a.delay === b.delay &&
  a.universe === b.universe &&
  a.instrumentType === b.instrumentType

/** `×1.4` */
export const multiplier = (v: number | null | undefined) =>
  isNum(v) ? `×${fmt.ratio(v, 1)}` : DASH

/** Client-side sort for tables the backend returns whole. Absent values last. */
/** Numbers inside text in their own order: TOP200 before TOP1000, model2 before model10. */
const NATURAL = new Intl.Collator('en-US', { numeric: true })

export function sortRows<T>(rows: T[], sort: Sort): T[] {
  const key = sort.key as keyof T
  return [...rows].sort((a, b) => {
    const x = a[key]
    const y = b[key]
    if (x == null) return y == null ? 0 : 1
    if (y == null) return -1
    const c =
      typeof x === 'string' && typeof y === 'string'
        ? NATURAL.compare(x, y)
        : x < y
          ? -1
          : x > y
            ? 1
            : 0
    return sort.desc ? -c : c
  })
}

/** `themes` is JSON array text of strings or `{id,name}` objects. */
export function parseThemes(raw: string | null): string[] {
  if (!raw) return []
  try {
    const list: unknown = JSON.parse(raw)
    if (!Array.isArray(list)) return []
    return list.map((t) =>
      typeof t === 'string'
        ? t
        : ((t as { name?: string; id?: string })?.name ??
          (t as { id?: string })?.id ??
          JSON.stringify(t)),
    )
  } catch {
    return [raw]
  }
}
