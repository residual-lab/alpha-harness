/**
 * Ticking datasets for a lab without leaving it: the Data Explorer's tree and availability
 * filters, with the fields they leave counted, and the way to the Explorer for the rest.
 */

import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { FilterIcon } from 'lucide-react'
import { type ReactNode, useMemo } from 'react'
import { catalog } from '@/api/catalog'
import type { Scope } from '@/api/types'
import { fmt } from '@/lib/format'
import { Button, ErrorNotice, Skeleton } from '@/ui/kit'
import { AvailabilityFilters } from './availability'
import { datasetNames } from './dataset-tree'
import { describeFilter, type FieldFilterState } from './state'
import { DatasetTree } from './tree-view'

export function DatasetPicker({
  scope,
  datasetIds,
  setDatasetIds,
  filter,
  setFilter,
  types,
  onMoreFilters,
  children,
}: {
  scope: Scope
  datasetIds: string[]
  setDatasetIds: (ids: string[]) => void
  filter: FieldFilterState
  setFilter: (change: (f: FieldFilterState) => FieldFilterState) => void
  /** Field types chosen by a control of the caller's own; left out, the filter's own count. */
  types?: string[]
  onMoreFilters: () => void
  /** The caller's own controls, given the fields of each type that match. */
  children?: (byType: Map<string, number> | null) => ReactNode
}) {
  const active: FieldFilterState = {
    ...filter,
    ...(types ? { field_types: types } : {}),
    dataset_ids: datasetIds,
  }
  // The market's whole tree, so ticking a category takes every dataset in it.
  const source = useQuery({
    queryKey: ['catalog', 'facets', scope, {}],
    queryFn: ({ signal }) => catalog.facets(scope, {}, signal),
  })
  const counts = useQuery({
    queryKey: ['catalog', 'facets', scope, active],
    queryFn: ({ signal }) => catalog.facets(scope, active, signal),
    placeholderData: keepPreviousData,
  })
  const datasets = useQuery({
    queryKey: ['catalog', 'datasets', scope, ''],
    queryFn: () => catalog.datasets(scope),
  })
  const names = useMemo(() => datasetNames(datasets.data ?? []), [datasets.data])
  const byType = counts.data ? new Map(counts.data.types.map((t) => [t.id, t.n])) : null
  const matching = datasetIds.length
    ? [...(byType?.entries() ?? [])]
        .filter(([type]) => !types || types.includes(type))
        .reduce((sum, [, n]) => sum + n, 0)
    : null
  // Coverage and the rest of the Data Explorer's filters, less what has a control here.
  const more = describeFilter(
    {
      ...filter,
      ...(types ? { field_types: [] } : {}),
      region_exclusive: false,
      region_agnostic: false,
    },
    scope.region,
  )

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="text-body text-ink">
          {matching === null ? (
            'Tick categories, subcategories or datasets.'
          ) : (
            <>
              <span className="num font-medium">{fmt.int(matching)}</span> field
              {matching === 1 ? '' : 's'} match
            </>
          )}
        </span>
        <Button size="sm" variant="ghost" onClick={onMoreFilters}>
          <FilterIcon />
          More Filters in Data Explorer
        </Button>
      </div>
      {more.length > 0 && (
        <p className="text-body-compact text-ink-subtle">Also narrowed by {more.join(' · ')}</p>
      )}
      {children?.(byType)}
      <AvailabilityFilters
        scope={scope}
        counts={counts.data?.availability}
        value={filter}
        onChange={(change) => setFilter((f) => ({ ...f, ...change }))}
      />
      {source.isError && <ErrorNotice error={source.error} title="Could not load the datasets" />}
      {source.data ? (
        <DatasetTree
          scope={scope}
          source={source.data}
          counts={counts.data}
          names={names}
          value={datasetIds}
          onChange={setDatasetIds}
        />
      ) : (
        !source.isError && <Skeleton className="h-40" />
      )}
    </div>
  )
}
