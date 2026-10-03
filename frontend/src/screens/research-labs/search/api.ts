/**
 * Search Lab: choose datasets, cores and simulations, then run the search as a task.
 * Request bodies are snake_case.
 */

import type { components } from '@/api/generated'
import { http } from '@/api/http'
import type { FieldFilterState } from '@/screens/data/state'

export interface SearchLabRequest {
  region: string
  delay: number
  universe?: string | null
  dataset_ids: string[]
  /** Only these fields of the datasets, when any were chosen. */
  field_ids: string[]
  /** The Data Explorer's filter the datasets were chosen under; `null` uses every field. */
  field_filter?: FieldFilterState | null
  vector_operators: string[]
  decay: number
  cores: number
  /** Needed to add a task; a preview ignores it. */
  simulations?: number
}

type Schemas = components['schemas']

export type SearchLabOptions = Schemas['Options']
export type SearchLabPreview = Schemas['Preview']

const B = '/api/search-lab'

export const searchLab = {
  /** Reads the account's operators, syncing them from BRAIN when missing. */
  options: () => http.get<SearchLabOptions>(`${B}/options`),
  /** Free; queues nothing. */
  preview: (body: SearchLabRequest) => http.post<SearchLabPreview>(`${B}/preview`, body),
  /** Adds the search to Tasks, not started. */
  addTask: (body: SearchLabRequest & { simulations: number }) =>
    http.post<Schemas['AddedTask']>(`${B}/tasks`, body),
}
