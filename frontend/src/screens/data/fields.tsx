/** Every field in the market: server-sorted, offset-paged, filtered; a row opens its detail. */

import { keepPreviousData, skipToken, useMutation, useQuery } from '@tanstack/react-query'
import { useNavigate } from '@tanstack/react-router'
import { CopyIcon, MaximizeIcon, MinimizeIcon, SparklesIcon } from 'lucide-react'
import { type RefObject, useEffect, useMemo, useRef, useState } from 'react'
import { toast } from 'sonner'
import {
  catalog,
  type DataFieldRow,
  type FieldAvailabilityRow,
  type FieldFilter,
  type FieldSortKey,
} from '@/api/catalog'
import { errorMessage } from '@/api/http'
import { type Scope, scopeLabel } from '@/api/types'
import { cn } from '@/lib/cn'
import { DASH, fmt } from '@/lib/format'
import { useDebounced } from '@/lib/use-debounced'
import { useDatasetPick, useFieldPick } from '@/screens/data/dataset-pick'
import { LAB_TABS } from '@/shell/nav'
import {
  Badge,
  Button,
  Chips,
  Disclosure,
  ErrorNotice,
  Fieldset,
  Input,
  KV,
  Metric,
  Panel,
  Skeleton,
} from '@/ui/kit'
import { Select, Sheet, SheetSteps } from '@/ui/overlay'
import { useMediaQuery } from '@/ui/panels'
import { type Column, DataTable, Pager, type Sort } from '@/ui/table'
import { AvailabilityFilters } from './availability'
import { datasetNames } from './dataset-tree'
import {
  describeFilter,
  endDatasetPick,
  type FieldFilterState,
  isActive,
  multiplier,
  parseThemes,
  RELEVANCE,
  STAT,
  sortRows,
  useDatasetChoice,
  useFieldFilter,
} from './state'
import { DatasetTree } from './tree-view'

const LIMIT = 100
/** The backend's own cap: enough for one focused idea, short enough for any chat LLM. */
const MAX_PICKED = 100
const TYPES = ['MATRIX', 'VECTOR', 'GROUP']

/** Two ways to read the search box, because neither answers the other's questions. */
const MODES = [
  {
    value: 'smart' as const,
    label: 'Smart',
    hint: 'Ranks whole words from the Field id and its Description, best match first.',
  },
  {
    value: 'text' as const,
    label: 'Exact',
    hint: 'Matches the letters you type, anywhere in the Field id or Description.',
  },
]

/** A segmented control: one mode lit, the other a way out of it. */
function SearchMode({
  value,
  onChange,
}: {
  value: string
  onChange: (mode: 'smart' | 'text') => void
}) {
  return (
    <div
      role="group"
      aria-label="Search mode"
      className="inline-flex h-9 shrink-0 overflow-hidden rounded-sm border border-hairline"
    >
      {MODES.map((mode) => (
        <button
          key={mode.value}
          type="button"
          title={mode.hint}
          aria-pressed={value === mode.value}
          onClick={() => onChange(mode.value)}
          className={cn(
            'px-3 text-body-compact transition-colors',
            value === mode.value
              ? 'bg-surface-3 font-medium text-ink'
              : 'text-ink-subtle hover:text-ink',
          )}
        >
          {mode.label}
        </button>
      ))}
    </div>
  )
}

/**
 * BRAIN's own Data Explorer columns, in its order and under its wording — plus Category, and
 * Users, which BRAIN still sends on every field but no longer draws. Against Alphas it gives
 * alphas per user: how hard the crowd is working a field, rather than how many have touched it.
 */
const COLUMNS: Column<DataFieldRow>[] = [
  {
    key: 'dataset_id',
    header: 'Dataset',
    width: 'minmax(68px,1fr)',
    sortable: true,
    cell: (r) => <Text value={r.dataset_id} mono className="text-ink-muted" />,
  },
  {
    key: 'field_id',
    header: 'Field',
    width: 'minmax(100px,1.5fr)',
    sortable: true,
    cell: (r) => <Text value={r.field_id} mono className="text-ink" />,
  },
  {
    key: 'description',
    header: 'Description',
    width: 'minmax(96px,2.4fr)',
    cell: (r) => <Text value={r.description} className="text-ink-muted" />,
  },
  {
    key: 'category_id',
    header: 'Category',
    width: 'minmax(92px,1.2fr)',
    sortable: true,
    cell: (r) => (
      <Text
        value={[r.category_name, r.subcategory_name].filter(Boolean).join(' / ') || null}
        className="text-ink-muted"
      />
    ),
  },
  {
    key: 'field_type',
    header: 'Type',
    width: 'minmax(68px,0.6fr)',
    sortable: true,
    cell: (r) => (
      <span
        className="num truncate text-body-compact text-ink-subtle"
        title={r.field_type ?? undefined}
      >
        {r.field_type ?? DASH}
      </span>
    ),
  },
  {
    key: 'pyramid_multiplier',
    header: 'Pyramid Theme Multiplier',
    width: 'minmax(88px,0.8fr)',
    align: 'right',
    sortable: true,
    cell: (r) => multiplier(r.pyramid_multiplier),
  },
  {
    key: 'coverage',
    header: 'Instrument Coverage',
    width: 'minmax(88px,0.8fr)',
    align: 'right',
    sortable: true,
    cell: (r) => fmt.pct(r.coverage),
  },
  {
    key: 'date_coverage',
    header: 'Date Coverage',
    width: 'minmax(80px,0.7fr)',
    align: 'right',
    sortable: true,
    cell: (r) => fmt.pct(r.date_coverage),
  },
  {
    key: 'user_count',
    header: 'Users',
    width: 'minmax(72px,0.5fr)',
    align: 'right',
    sortable: true,
    cell: (r) => fmt.int(r.user_count),
  },
  {
    key: 'alpha_count',
    header: 'Alphas',
    // Wide enough for a 7-digit count: `close` alone passes 700,000.
    width: 'minmax(92px,0.5fr)',
    align: 'right',
    sortable: true,
    cell: (r) => fmt.int(r.alpha_count),
  },
  {
    key: 'date_created',
    header: 'Date Added',
    width: 'minmax(108px,0.8fr)',
    align: 'right',
    sortable: true,
    cell: (r) => fmt.month(r.date_created),
  },
]

const ADVANCED: (keyof FieldFilterState)[] = [
  'dataset_ids',
  'coverage_min',
  'coverage_max',
  'alpha_count_min',
  'alpha_count_max',
  'user_count_min',
  'user_count_max',
  'pyramid_multiplier_min',
  'pyramid_multiplier_max',
  'date_coverage_min',
  'date_coverage_max',
  'date_created_from',
  'date_created_to',
]

/**
 * The columns a window this wide can hold without a horizontal scrollbar, shed in order of
 * what a narrowed row can least afford to lose. Users is never one of them: BRAIN stopped
 * drawing that column while still sending the number, and against Alphas it is the only
 * reading of how hard the crowd is working a field. Everything shed stays in the row's
 * detail sheet.
 */
function useFittingColumns(): Column<DataFieldRow>[] {
  const roomForCategory = useMediaQuery('(min-width: 1340px)')
  const roomForType = useMediaQuery('(min-width: 1212px)')
  const roomForDataset = useMediaQuery('(min-width: 1084px)')
  return useMemo(() => {
    const dropped = new Set(
      [
        !roomForCategory && 'category_id',
        !roomForType && 'field_type',
        !roomForDataset && 'dataset_id',
      ].filter(Boolean),
    )
    return COLUMNS.filter((c) => !dropped.has(c.key))
  }, [roomForCategory, roomForType, roomForDataset])
}

/**
 * The panel on its own, filling the display. Deep dataset research wants every pixel, and the
 * browser's Fullscreen API is the only thing that can take the space the window chrome holds.
 */
function useFullscreen() {
  const ref = useRef<HTMLDivElement>(null)
  const [on, setOn] = useState(false)
  useEffect(() => {
    const sync = () => setOn(document.fullscreenElement === ref.current)
    document.addEventListener('fullscreenchange', sync)
    return () => document.removeEventListener('fullscreenchange', sync)
  }, [])
  const toggle = () => {
    if (document.fullscreenElement) {
      void document.exitFullscreen()
      return
    }
    // The browser refuses outside a user gesture or under a permissions policy; say so rather
    // than leaving a button that looks broken.
    ref.current?.requestFullscreen().catch((e: unknown) => {
      toast.error('Full screen was refused', {
        description: e instanceof Error ? e.message : undefined,
      })
    })
  }
  return { ref, on, toggle }
}

export function FieldsTab({ scope }: { scope: Scope }) {
  const { filter, sort, offset, setSort, page } = useFieldFilter()
  const [datasetIds] = useDatasetChoice()
  const columns = useFittingColumns()
  const full = useFullscreen()
  const active: FieldFilterState = { ...filter, dataset_ids: datasetIds }
  const [openId, setOpenId] = useState<string | null>(null)
  const [picked, setPicked] = useState<ReadonlySet<string>>(() => new Set())
  const pick = (ids: string[], on: boolean) => {
    const next = new Set(picked)
    for (const id of ids) {
      if (on) next.add(id)
      else next.delete(id)
    }
    if (next.size > MAX_PICKED) {
      toast.error(`Select at most ${MAX_PICKED} fields at once`)
      return
    }
    setPicked(next)
  }
  const copy = useMutation({
    mutationFn: async () => {
      const outline = await catalog.outline(scope, { field_ids: [...picked] })
      await navigator.clipboard.writeText(outline.text)
      return outline
    },
    onSuccess: (outline) =>
      toast.success(`Copied ${fmt.int(picked.size - outline.missing.length)} Data Fields`, {
        description: outline.missing.length
          ? `Not in this market, so left out: ${outline.missing.join(', ')}`
          : 'Grouped by Category, Subcategory and Dataset, ready to paste into any LLM.',
      }),
    onError: (e) => toast.error('Could not copy the Data Fields', { description: errorMessage(e) }),
  })

  // A new market starts at its first page.
  const label = scopeLabel(scope)
  const seen = useRef(label)
  useEffect(() => {
    if (seen.current !== label) {
      seen.current = label
      page(0)
      setPicked(new Set())
    }
  }, [label, page])

  const body: FieldFilter = {
    ...active,
    sort_by: sort.key as FieldSortKey,
    sort_desc: sort.desc,
    limit: LIMIT,
    offset,
  }
  const query = useQuery({
    queryKey: ['catalog', 'fields', scope, body],
    queryFn: ({ signal }) => catalog.fields(scope, body, signal),
    placeholderData: keepPreviousData,
  })
  const filtered = Object.values(active).some(isActive)
  const picking = useDatasetPick((s) => s.active)
  const labFields = useFieldPick((s) => s.fields)
  const forLab = useMemo(() => new Set(Object.keys(labFields)), [labFields])
  const pickForLab = (ids: string[], on: boolean) => {
    const datasetOf = new Map(query.data?.results.map((r) => [r.field_id, r.dataset_id ?? '']))
    useFieldPick.setState(({ fields }) => {
      const next = { ...fields }
      for (const id of ids) {
        if (on) next[id] = datasetOf.get(id) ?? next[id] ?? ''
        else delete next[id]
      }
      return { fields: next }
    })
  }

  return (
    <>
      {picking && <PickBar matching={query.data?.total} />}
      {/* The fullscreen element paints its own ground: the page behind it is gone, and an
        unpainted one shows through as the browser's default black. */}
      <div ref={full.ref} className={cn(full.on && 'h-full overflow-auto bg-canvas p-4')}>
        <Panel
          className={cn(full.on && 'rounded-none border-0')}
          title="Fields"
          actions={
            <>
              {picked.size > 0 && (
                <>
                  <Button variant="ghost" size="sm" onClick={() => setPicked(new Set())}>
                    Clear
                  </Button>
                  <Button size="sm" onClick={() => copy.mutate()} disabled={copy.isPending}>
                    <CopyIcon aria-hidden />
                    Copy Selected Data Fields ({fmt.int(picked.size)})
                  </Button>
                </>
              )}
              <Button variant="ghost" size="sm" onClick={full.toggle}>
                {full.on ? <MinimizeIcon /> : <MaximizeIcon />}
                {full.on ? 'Exit Full Screen' : 'Full Screen'}
              </Button>
              {query.data && (
                <span className={STAT}>
                  <span className="num text-ink">{fmt.int(query.data.total)}</span>
                  fields
                </span>
              )}
            </>
          }
        >
          <div className="flex flex-col gap-4">
            <FieldFilters scope={scope} />
            {query.isError && (query.data?.results.length ?? 0) > 0 && (
              <ErrorNotice error={query.error} title="Could not load fields" />
            )}
            <DataTable
              label="Data fields"
              rows={query.data?.results ?? []}
              columns={columns}
              rowKey={(r) => r.field_id}
              sort={sort}
              onSort={setSort}
              onRowClick={(r) => setOpenId(r.field_id)}
              activeKey={openId}
              page={offset}
              // While a lab picks datasets, its ticks are the only ones on the page.
              // While a lab picks, a tick hands it that field; otherwise it is for copying.
              {...(picking
                ? { selected: forLab, onSelect: pickForLab }
                : { selected: picked, onSelect: pick })}
              loading={query.isPending || query.isPlaceholderData}
              error={query.error}
              maxHeight={full.on ? 'calc(100vh - 17rem)' : undefined}
              empty={
                filtered
                  ? 'No fields match these filters.'
                  : 'No fields in this market yet. Download it in BRAIN › Sync.'
              }
            />
            {query.data && (
              <Pager total={query.data.total} offset={offset} limit={LIMIT} onChange={page} />
            )}
          </div>
          <FieldSheet
            scope={scope}
            id={openId}
            ids={(query.data?.results ?? []).map((r) => r.field_id)}
            onStep={setOpenId}
            onClose={() => setOpenId(null)}
            container={full.on ? full.ref : undefined}
          />
        </Panel>
      </div>
    </>
  )
}

/** While a lab picks datasets: what is ticked, what it leaves, and the way back to the lab. */
function PickBar({ matching }: { matching: number | undefined }) {
  const navigate = useNavigate()
  const count = useDatasetPick((s) => s.ids.length)
  const fields = useFieldPick((s) => Object.keys(s.fields).length)
  const from = useDatasetPick((s) => s.from)
  const back = (done: boolean) => {
    // The filter goes with the datasets: the lab searches only the fields it shows.
    endDatasetPick(done)
    void navigate({ to: from })
  }

  return (
    <div className="sticky top-0 z-10 flex flex-wrap items-center gap-3 rounded-lg border border-hairline bg-surface-1 p-3">
      <Metric boxed size="sm" label="Datasets Selected" value={fmt.int(count)} />
      {fields > 0 ? (
        <Metric boxed size="sm" label="Fields Selected" value={fmt.int(fields)} />
      ) : (
        <Metric boxed size="sm" label="Matching Fields" value={fmt.int(count ? matching : null)} />
      )}
      <span className="min-w-0 flex-1 text-body-compact text-pretty text-ink-subtle">
        Tick whole categories, subcategories or datasets in More Filters, or single fields in the
        table: with fields ticked, the lab uses only those.
      </span>
      <Button variant="ghost" onClick={() => back(false)}>
        Cancel
      </Button>
      <Button variant="primary" disabled={count === 0 && fields === 0} onClick={() => back(true)}>
        Use in {LAB_TABS.find((lab) => lab.to === from)?.label ?? 'the Lab'}
      </Button>
    </div>
  )
}

function FieldFilters({ scope }: { scope: Scope }) {
  const { filter, set, replace, sort, rank } = useFieldFilter()
  const [datasetIds, setDatasetIds] = useDatasetChoice()
  const picking = useDatasetPick((s) => s.active)
  const active: FieldFilterState = { ...filter, dataset_ids: datasetIds }
  const facets = useQuery({
    queryKey: ['catalog', 'facets', scope, active],
    queryFn: ({ signal }) => catalog.facets(scope, active, signal),
    placeholderData: keepPreviousData,
  })
  // The market's whole tree whatever else is filtered, so ticking a category takes every dataset in it.
  const tree = useQuery({
    queryKey: ['catalog', 'facets', scope, {}],
    queryFn: ({ signal }) => catalog.facets(scope, {}, signal),
  })
  const datasets = useQuery({
    queryKey: ['catalog', 'datasets', scope, ''],
    queryFn: () => catalog.datasets(scope),
  })
  const names = useMemo(() => datasetNames(datasets.data ?? []), [datasets.data])
  const categoryNames = useMemo(
    () => new Map((tree.data?.categories ?? []).map((c) => [c.id, c.name ?? c.id])),
    [tree.data],
  )
  const stats = useQuery({
    queryKey: ['catalog', 'stats', scope],
    queryFn: () => catalog.stats(scope),
  })

  /**
   * A Pyramid Multiplier cell hands over a category; the Datasets tree speaks dataset ids.
   * Translating as soon as this market's tree is known leaves one selection in one place —
   * ticked in the tree and summarised as "Other" — rather than two that disagree.
   */
  const chosenCategories = filter.category_ids
  const [landed, setLanded] = useState(false)
  useEffect(() => {
    const wanted = chosenCategories ?? []
    if (!tree.data || wanted.length === 0) return
    const ids = tree.data.datasets
      .filter((d) => d.category_id !== null && wanted.includes(d.category_id))
      .map((d) => d.id)
    // Nothing to tick: leave the category filter alone, so the table stays narrowed and the
    // chip keeps saying what narrowed it.
    if (ids.length === 0) return
    useFieldFilter.getState().set({ category_ids: [], dataset_ids: ids })
    setLanded(true)
  }, [tree.data, chosenCategories])

  // Ranking is only on offer while there is a smart search to rank against.
  const ranked = !!filter.search && (filter.search_mode ?? 'smart') === 'smart'

  // The search box types freely; the query follows a beat later.
  const [search, setSearch] = useState(filter.search ?? '')
  const term = useDebounced(search.trim(), 250)
  useEffect(() => {
    if ((useFieldFilter.getState().filter.search ?? '') !== term) set({ search: term || null })
  }, [term, set])

  // Counts follow every other filter; a chosen type stays listed even when nothing else matches it.
  const typeCounts = new Map(facets.data?.types.map((t) => [t.id, t.n]))
  const types = [
    ...new Set([
      ...(facets.data ? facets.data.types.map((t) => t.id) : TYPES),
      ...(filter.field_types ?? []),
    ]),
  ]
  const advancedOn = ADVANCED.filter((k) => isActive(active[k])).length
  // What More Filters narrows by, said on it while it is closed.
  const narrowed = [
    ...(datasetIds.length
      ? [`${fmt.int(datasetIds.length)} Dataset${datasetIds.length === 1 ? '' : 's'}`]
      : []),
    ...describeFilter(
      Object.fromEntries(ADVANCED.map((k) => [k, active[k]])) as FieldFilterState,
      scope.region,
    ),
  ]
  const s = stats.data

  // A lab choosing datasets lands here: More Filters opens, lit, and scrolls into view.
  const more = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (picking) more.current?.scrollIntoView({ block: 'start', behavior: 'smooth' })
  }, [picking])

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <SearchMode
          value={filter.search_mode ?? 'smart'}
          onChange={(mode) => set({ search_mode: mode })}
        />
        <Input
          className="w-full sm:w-64"
          placeholder="Search Field ID or Description"
          aria-label="Search fields"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        {/* Ranking has no column to click, so without this there is no way back to it once a
            column has been chosen — and no sign it was ever what the table was ordered by. */}
        {ranked && (
          <Button
            variant={sort.key === RELEVANCE ? 'secondary' : 'ghost'}
            size="sm"
            aria-pressed={sort.key === RELEVANCE}
            onClick={rank}
            title="Order by how well each Field answers the search"
          >
            <SparklesIcon />
            Best Match
          </Button>
        )}
        <Chips
          label="Field type"
          value={filter.field_types ?? []}
          onChange={(v) => set({ field_types: v })}
          items={types.map((t) => ({
            value: t,
            label: (
              <span className="num">
                {t}{' '}
                <span className="text-ink-subtle">
                  {fmt.int(facets.data ? (typeCounts.get(t) ?? 0) : null)}
                </span>
              </span>
            ),
          }))}
        />
        {/* Only ever arrived at from the Pyramid Multiplier Map, so it shows only when set —
            but it has to show, or the table is narrowed by something invisible. */}
        {(filter.category_ids?.length ?? 0) > 0 && (
          <Chips
            label="Category"
            value={filter.category_ids ?? []}
            onChange={(v) => set({ category_ids: v })}
            items={(filter.category_ids ?? []).map((id) => ({
              value: id,
              title: 'Remove this Category filter',
              label: (
                <>
                  {categoryNames.get(id) ?? id} <span className="text-ink-subtle">×</span>
                </>
              ),
            }))}
          />
        )}
        <Button
          variant="ghost"
          size="sm"
          disabled={!Object.values(active).some(isActive) && !search}
          onClick={() => {
            setSearch('')
            replace({})
            setDatasetIds([])
          }}
        >
          Reset filters
        </Button>
      </div>
      <AvailabilityFilters scope={scope} counts={facets.data?.availability} />
      {facets.isError && <ErrorNotice error={facets.error} title="Could not load filter choices" />}
      {stats.isError && <ErrorNotice error={stats.error} title="Could not load field statistics" />}
      {datasets.isError && (
        <ErrorNotice error={datasets.error} title="Could not load dataset names" />
      )}
      {tree.isError && (
        <ErrorNotice
          error={tree.error}
          title="Could not load this market's categories and datasets"
        />
      )}

      <div ref={more} className="scroll-mt-24">
        <Disclosure
          defaultOpen={picking || landed || undefined}
          className={cn(picking && 'border-primary ring-1 ring-primary-subtle')}
          summary={
            <>
              More Filters
              {advancedOn > 0 && <Badge className="num">{advancedOn}</Badge>}
              {narrowed.length > 0 && (
                <span className="min-w-0 truncate text-body-compact text-ink-subtle group-open:hidden">
                  {narrowed.join(' · ')}
                </span>
              )}
            </>
          }
        >
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
            <div className="min-w-0 md:col-span-2 xl:col-span-3">
              {tree.data ? (
                <DatasetTree
                  scope={scope}
                  source={tree.data}
                  counts={facets.data}
                  names={names}
                  value={datasetIds}
                  onChange={setDatasetIds}
                />
              ) : (
                !tree.isError && <Skeleton className="h-40" />
              )}
            </div>
            {/* In the table's own column order, so a filter sits where its column does. Each
                box shows this market's own bound until something is typed in it. */}
            <Range
              label="Pyramid Theme Multiplier"
              bounds={s && [ratio(s.pyramid_multiplier_min), ratio(s.pyramid_multiplier_max)]}
              step={0.1}
              min={filter.pyramid_multiplier_min}
              max={filter.pyramid_multiplier_max}
              onChange={(pyramid_multiplier_min, pyramid_multiplier_max) =>
                set({ pyramid_multiplier_min, pyramid_multiplier_max })
              }
            />
            <Range
              label="Instrument Coverage (%)"
              bounds={s && [percent(s.coverage_min), percent(s.coverage_max)]}
              scale={100}
              min={filter.coverage_min}
              max={filter.coverage_max}
              onChange={(coverage_min, coverage_max) => set({ coverage_min, coverage_max })}
            />
            <Range
              label="Date Coverage (%)"
              bounds={s && [percent(s.date_coverage_min), percent(s.date_coverage_max)]}
              scale={100}
              min={filter.date_coverage_min}
              max={filter.date_coverage_max}
              onChange={(date_coverage_min, date_coverage_max) =>
                set({ date_coverage_min, date_coverage_max })
              }
            />
            <Range
              label="Users"
              bounds={s && [count(s.user_count_min), count(s.user_count_max)]}
              min={filter.user_count_min}
              max={filter.user_count_max}
              onChange={(user_count_min, user_count_max) => set({ user_count_min, user_count_max })}
            />
            <Range
              label="Alphas"
              bounds={s && [count(s.alpha_count_min), count(s.alpha_count_max)]}
              min={filter.alpha_count_min}
              max={filter.alpha_count_max}
              onChange={(alpha_count_min, alpha_count_max) =>
                set({ alpha_count_min, alpha_count_max })
              }
            />
            <MonthRange
              label="Date Added"
              months={s?.date_added ?? []}
              counts={facets.data?.date_added}
              from={filter.date_created_from}
              to={filter.date_created_to}
              onChange={(date_created_from, date_created_to) =>
                set({ date_created_from, date_created_to })
              }
            />
          </div>
        </Disclosure>
      </div>
    </div>
  )
}

function NumberBox({
  label,
  value,
  onChange,
  scale = 1,
  step,
  placeholder,
}: {
  label: string
  value: number | null | undefined
  onChange: (value: number | null) => void
  scale?: number | undefined
  step?: number
  placeholder?: string
}) {
  const shown = value == null ? '' : String(+(value * scale).toFixed(6))
  const [text, setText] = useState(shown)
  // Passed on once typing pauses, so "0.75" asks for the fields once rather than per key.
  const [waiting, setWaiting] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined)
  useEffect(() => () => clearTimeout(timer.current), [])
  // Typed text stands while it still means the number held above: "1.0" typed on the way to
  // "1.05" parses to 1, and rewriting the box to "1" would eat the zero the user just typed.
  const same = waiting || (text === '' ? value == null : Number(text) / scale === value)
  return (
    <Input
      type="number"
      min={0}
      step={step}
      aria-label={label}
      placeholder={placeholder}
      value={same ? text : shown}
      onChange={(e) => {
        const typed = e.target.value
        setText(typed)
        setWaiting(true)
        clearTimeout(timer.current)
        timer.current = setTimeout(() => {
          setWaiting(false)
          onChange(typed === '' ? null : Number(typed) / scale)
        }, 400)
      }}
    />
  )
}

/** A bound as a box shows it: a plain number, never a unit, and empty when unknown. */
const ratio = (v: number | null | undefined) => (v == null ? '' : v.toFixed(1))
const percent = (v: number | null | undefined) => (v == null ? '' : String(Math.round(v * 100)))
const count = (v: number | null | undefined) => (v == null ? '' : fmt.int(v))

function Range({
  label,
  bounds,
  min,
  max,
  onChange,
  scale,
  step,
}: {
  label: string
  /** This market's lowest and highest, shown in the empty boxes. */
  bounds?: [string, string] | null | undefined
  min: number | null | undefined
  max: number | null | undefined
  onChange: (min: number | null, max: number | null) => void
  scale?: number | undefined
  step?: number
}) {
  return (
    <Fieldset legend={label}>
      <div className="grid grid-cols-2 gap-2">
        <NumberBox
          label={`${label} minimum`}
          placeholder={bounds?.[0] || 'min'}
          scale={scale}
          {...(step === undefined ? {} : { step })}
          value={min}
          onChange={(v) => onChange(v, max ?? null)}
        />
        <NumberBox
          label={`${label} maximum`}
          placeholder={bounds?.[1] || 'max'}
          scale={scale}
          {...(step === undefined ? {} : { step })}
          value={max}
          onChange={(v) => onChange(min ?? null, v)}
        />
      </div>
    </Fieldset>
  )
}

const ANY = 'any'

/**
 * From and To months, both inclusive. BRAIN dates a field by month, so the choices are the
 * months this market actually has, each counted under the other filters, as the table would
 * show it; neither end can pass the other.
 */
/** Starts the Date Added filter offers in one click, counted back from this month. */
const SINCE = [
  { label: 'Last month', back: 1 },
  { label: 'Last 3 months', back: 3 },
  { label: 'Last 6 months', back: 6 },
  { label: 'Last year', back: 12 },
] as const

/** The first day of the month `back` months before this one, as BRAIN dates a month. */
function monthsAgo(back: number): string {
  const now = new Date()
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - back, 1))
    .toISOString()
    .slice(0, 10)
}

function MonthRange({
  label,
  months,
  counts,
  from,
  to,
  onChange,
}: {
  label: string
  /** Every month this market has, so a month the other filters empty stays choosable. */
  months: { month: string; fields: number }[]
  /** Fields per month under the other filters: what choosing each would show. */
  counts: { month: string; fields: number }[] | undefined
  from: string | null | undefined
  to: string | null | undefined
  onChange: (from: string | null, to: string | null) => void
}) {
  const shown = new Map(counts?.map((c) => [c.month, c.fields]))
  const fieldsIn = (m: { month: string; fields: number }) =>
    counts ? (shown.get(m.month) ?? 0) : m.fields
  const choices = (keep: (month: string) => boolean) => [
    { value: ANY, label: 'Any' },
    ...months
      .filter((m) => keep(m.month))
      .map((m) => ({ value: m.month, label: `${fmt.month(m.month)} (${fmt.int(fieldsIn(m))})` })),
  ]
  const pick = (value: string) => (value === ANY ? null : value)
  return (
    <Fieldset legend={label}>
      <div className="grid grid-cols-2 gap-2">
        <Select
          label={`${label} from`}
          className="w-full"
          value={from ?? ANY}
          items={choices((m) => !to || m <= to)}
          onChange={(v) => onChange(pick(v), to ?? null)}
          disabled={months.length === 0}
        />
        <Select
          label={`${label} to`}
          className="w-full"
          value={to ?? ANY}
          items={choices((m) => !from || m >= from)}
          onChange={(v) => onChange(from ?? null, pick(v))}
          disabled={months.length === 0}
        />
      </div>
      <div className="mt-2 flex flex-wrap gap-1">
        {SINCE.map(({ label: since, back }) => {
          // The first month on offer that recent: a start outside the list would leave the
          // From menu showing nothing, and none at all means nothing here is that new.
          const start = monthsAgo(back)
          const first = months.find((m) => m.month >= start)?.month
          const on = first !== undefined && from === first && !to
          return (
            <Button
              key={since}
              size="sm"
              variant={on ? 'secondary' : 'ghost'}
              aria-pressed={on}
              disabled={first === undefined}
              onClick={() => first && onChange(first, null)}
            >
              {since}
            </Button>
          )
        })}
      </div>
    </Fieldset>
  )
}

const AVAILABILITY_COLUMNS: Column<FieldAvailabilityRow>[] = [
  {
    key: 'region',
    header: 'Region',
    width: '80px',
    cell: (r) => <span className="num">{r.region}</span>,
  },
  {
    key: 'delay',
    header: 'Delay',
    width: '64px',
    cell: (r) => <span className="num">{r.delay}</span>,
  },
  {
    key: 'universe',
    header: 'Universe',
    width: 'minmax(110px,1fr)',
    cell: (r) => <span className="num">{r.universe}</span>,
  },
  {
    key: 'coverage',
    header: 'Coverage',
    width: '96px',
    align: 'right',
    cell: (r) => fmt.pct(r.coverage),
  },
  {
    key: 'alpha_count',
    header: 'Alpha Count',
    width: '112px',
    align: 'right',
    sortable: true,
    cell: (r) => fmt.int(r.alpha_count),
  },
]

function FieldSheet({
  scope,
  id,
  ids,
  onStep,
  onClose,
  container,
}: {
  scope: Scope
  id: string | null
  /** The fields on this page, in the order shown, to step through. */
  ids: string[]
  onStep: (id: string) => void
  onClose: () => void
  /** While the panel is fullscreen, the sheet has to live inside it to be drawn at all. */
  container?: RefObject<HTMLElement | null> | undefined
}) {
  const detail = useQuery({
    queryKey: ['catalog', 'field', scope, id],
    queryFn: id == null ? skipToken : () => catalog.field(scope, id),
  })
  const availability = useQuery({
    queryKey: ['catalog', 'availability', id],
    queryFn: id == null ? skipToken : () => catalog.availability(id),
  })
  const [availabilitySort, setAvailabilitySort] = useState<Sort>({
    key: 'alpha_count',
    desc: true,
  })
  const availabilityRows = useMemo(
    () => sortRows(availability.data ?? [], availabilitySort),
    [availability.data, availabilitySort],
  )
  const d = detail.data
  const themes = parseThemes(d?.themes ?? null)
  // While a lab picks datasets, a field read here can bring its dataset into the pick.
  const picking = useDatasetPick((s) => s.active)
  const [chosen, choose] = useDatasetChoice()
  const dataset = d?.dataset_id
  const ticked = dataset != null && chosen.includes(dataset)

  return (
    <Sheet
      open={id != null}
      onOpenChange={(open) => !open && onClose()}
      container={container}
      title={<span className="num">{id}</span>}
      actions={<SheetSteps ids={ids} id={id} onStep={onStep} noun="Field" />}
      description={d?.description ?? (detail.isPending ? 'Loading…' : 'No description.')}
    >
      <div className="flex flex-col gap-4">
        {detail.isError ? (
          <ErrorNotice error={detail.error} title="Could not load this field" />
        ) : !d ? (
          <Skeleton className="h-56" />
        ) : (
          <div className="flex flex-col gap-3">
            {picking && dataset && (
              <Button
                size="sm"
                variant={ticked ? 'secondary' : 'primary'}
                className="self-start"
                onClick={() =>
                  choose(ticked ? chosen.filter((x) => x !== dataset) : [...chosen, dataset])
                }
              >
                {ticked ? `Untick Dataset ${dataset}` : `Tick Dataset ${dataset}`}
              </Button>
            )}
            <KV
              items={[
                ['Dataset', d.dataset_id ?? DASH],
                ['Category', d.category_name ?? DASH],
                ['Subcategory', d.subcategory_name ?? DASH],
                ['Type', d.field_type ?? DASH],
                ['Instrument Coverage', fmt.pct(d.coverage)],
                ['Date Coverage', fmt.pct(d.date_coverage)],
                ['Alpha Count', fmt.int(d.alpha_count)],
                ['User Count', fmt.int(d.user_count)],
                ['Pyramid Theme Multiplier', multiplier(d.pyramid_multiplier)],
                ['Scope', `${d.region} · D${d.delay} · ${d.universe}`],
                ['Date Added', fmt.month(d.date_created)],
                ['Downloaded', fmt.dateTime(d.synced_at)],
              ]}
            />
            {themes.length > 0 && (
              <div className="flex flex-wrap gap-1">
                {themes.map((t) => (
                  <Badge key={t} tone="outline">
                    {t}
                  </Badge>
                ))}
              </div>
            )}
          </div>
        )}

        <section className="flex flex-col gap-2">
          <h3 className="text-title">Available in</h3>
          {availability.isError ? (
            <ErrorNotice error={availability.error} title="Could not check availability" />
          ) : (
            <DataTable
              label="Field availability"
              rows={availabilityRows}
              columns={AVAILABILITY_COLUMNS}
              sort={availabilitySort}
              onSort={setAvailabilitySort}
              rowKey={(r) => `${r.instrument_type}/${r.region}/${r.delay}/${r.universe}`}
              loading={availability.isPending}
              maxHeight="40vh"
              empty="No downloaded market has this field."
            />
          )}
        </section>
      </div>
    </Sheet>
  )
}

/** One-line text cut to width, full text on hover. */
export function Text({
  value,
  mono,
  className,
}: {
  value: string | null | undefined
  mono?: boolean
  className?: string
}) {
  if (!value) return <span className="text-ink-subtle">{DASH}</span>
  return (
    <span title={value} className={cn('truncate', mono && 'num', className)}>
      {value}
    </span>
  )
}
