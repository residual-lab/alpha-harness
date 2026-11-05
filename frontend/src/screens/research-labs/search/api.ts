/**
 * Search Lab: choose datasets, cores and simulations, then run the search as a task.
 * Request bodies are snake_case.
 */

import type { components } from '@/api/generated'
import { http } from '@/api/http'
import type { FieldFilterState } from '@/screens/data/state'

type Schemas = components['schemas']

/** The backend's own request, so a field it gains cannot go missing here. Two differ: the
 *  generated type requires every field with a default, so the Data Explorer's filter, sent
 *  without its paging and sort, keeps its own type; and `simulations` is only needed to add a
 *  task, as a preview ignores it. */
export type SearchLabRequest = Omit<Schemas['SearchRequest'], 'simulations' | 'field_filter'> & {
  /** The Data Explorer's filter the datasets were chosen under; `null` uses every field. */
  field_filter?: FieldFilterState | null
  simulations?: number
}

export type SearchLabOptions = Schemas['Options']
export type SearchLabPreview = Schemas['Preview']

const B = '/api/search-lab'

export const searchLab = {
  /** Reads the account's operators, syncing them from BRAIN when missing. */
  options: () => http.get<SearchLabOptions>(`${B}/options`),
  /** Free; queues nothing. */
  preview: (body: SearchLabRequest, signal?: AbortSignal) =>
    http.post<SearchLabPreview>(`${B}/preview`, body, signal),
  /** Adds the search to Tasks, not started. */
  addTask: (body: SearchLabRequest & { simulations: number }) =>
    http.post<Schemas['AddedTask']>(`${B}/tasks`, body),
}
