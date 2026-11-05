/**
 * The local data-field catalog (Data Explorer). Rows from DuckDB stay snake_case; request
 * bodies are snake_case. `/counts /stats /facets /tree /fields /datasets` read the scope
 * from the query string with `instrumentType`.
 */

import type { components } from './generated'
import { http, qs } from './http'
import { type Scope, scopeQs } from './types'

type Schemas = components['schemas']

/** A market a full sync covers (`GET /api/catalog/markets`). */
export type Market = Schemas['Market']
export type CatalogScopeRow = Schemas['CatalogScopeRow']
export type CatalogSize = Schemas['CatalogSize']
export type CatalogCounts = Schemas['CatalogCounts']
export type CatalogStats = Schemas['CatalogStats']
export type CatalogFacets = Schemas['CatalogFacets']
export type DataFieldRow = Schemas['DataFieldRow']
export type FieldPage = Schemas['FieldPage']
export type DataFieldDetail = Schemas['DataFieldDetail']
export type FieldAvailabilityRow = Schemas['FieldAvailabilityRow']
export type DatasetRow = Schemas['DatasetRow']
export type PyramidGridData = Schemas['PyramidGrid']
export type FieldOutline = Schemas['FieldOutline']

/** Unknown keys sort by alpha_count on the backend. */
export type FieldSortKey =
  | 'field_id'
  | 'dataset_id'
  | 'category_id'
  | 'coverage'
  | 'user_count'
  | 'alpha_count'
  | 'pyramid_multiplier'
  | 'field_type'
  | 'date_coverage'
  | 'date_created'
  /** Not a column: how well the row answers the search. */
  | 'relevance'

/** The backend's own filter, every field optional, with the sort key narrowed to the columns
 * it will actually sort on. */
export type FieldFilter = Omit<Partial<Schemas['FieldFilter']>, 'sort_by'> & {
  sort_by?: FieldSortKey
}

const B = '/api/catalog'

export const catalog = {
  scopes: () => http.get<CatalogScopeRow[]>(`${B}/scopes`),
  size: () => http.get<CatalogSize>(`${B}/size`),
  syncAll: () => http.post<Schemas['SyncAllRun']>(`${B}/sync-all`),
  /** Region ALL only: its own download, because BRAIN pages it fifty fields at a time. */
  syncRegionAgnostic: () => http.post<Schemas['SyncAllRun']>(`${B}/sync-region-agnostic`),
  markets: () => http.get<Market[]>(`${B}/markets`),
  cancel: (id: number) => http.post<Schemas['Cancelled']>(`${B}/sync/runs/${id}/cancel`),

  counts: (s: Scope) => http.get<CatalogCounts>(`${B}/counts${scopeQs(s)}`),
  stats: (s: Scope) => http.get<CatalogStats>(`${B}/stats${scopeQs(s)}`),
  /** Counts under every other active filter; each facet ignores its own selection. */
  facets: (s: Scope, filter: FieldFilter, signal?: AbortSignal) =>
    http.post<CatalogFacets>(`${B}/facets${scopeQs(s)}`, filter, signal),
  fields: (s: Scope, filter: FieldFilter, signal?: AbortSignal) =>
    http.post<FieldPage>(`${B}/fields${scopeQs(s)}`, filter, signal),
  field: (s: Scope, id: string) =>
    http.get<DataFieldDetail>(`${B}/fields/${encodeURIComponent(id)}${scopeQs(s)}`),
  /** What a field is, from whichever market covers it best: for what is not market-bound. */
  fieldAnywhere: (id: string) =>
    http.get<DataFieldDetail>(`${B}/fields/${encodeURIComponent(id)}/anywhere`),
  /** Fields named or described by ``search`` in any market, each once, the most used first. */
  searchAnywhere: (search: string, limit = 40) =>
    http.get<DataFieldRow[]>(`${B}/field-search${qs({ search, limit })}`),
  availability: (id: string) =>
    http.get<FieldAvailabilityRow[]>(`${B}/fields/${encodeURIComponent(id)}/availability`),
  datasets: (s: Scope) => http.get<DatasetRow[]>(`${B}/datasets${scopeQs(s)}`),

  pyramids: () => http.get<PyramidGridData>(`${B}/pyramids`),
  /** The chosen fields as Category › Subcategory › Dataset › Field text, for an LLM. */
  outline: (s: Scope, pick: { field_ids: string[] } | { dataset_ids: string[] }) =>
    http.post<FieldOutline>(`${B}/fields/outline${scopeQs(s)}`, pick),
}
