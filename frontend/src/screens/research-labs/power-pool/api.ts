/** LLM Power Pool Lab: datasets, a model, cores and simulations, then add the task to Tasks. */

import type { components } from '@/api/generated'
import { http } from '@/api/http'
import type { FieldFilterState } from '@/screens/data/state'

type Schemas = components['schemas']

export type PowerPoolOptions = Schemas['PowerPoolOptions']
export type PowerPoolPreview = Schemas['PowerPoolPreview']

export interface PowerPoolRequest {
  region: string
  delay: number
  universe: string
  dataset_ids: string[]
  /** The Data Explorer's filter the datasets were chosen under; `null` uses every field. */
  field_filter?: FieldFilterState | null
  model: string | null
  /** Wait for `model` alone instead of moving on to another model when it cannot answer. */
  only_model: boolean
  /** Empty keeps every neutralization BRAIN offers for the market. */
  neutralizations: string[]
  /** Empty draws from every downloaded universe of the market. */
  universes: string[]
  cores: number
  simulations: number
}

const B = '/api/power-pool-lab'

export const powerPoolLab = {
  options: () => http.get<PowerPoolOptions>(`${B}/options`),
  /** Free: no LLM call, no simulation. */
  preview: (body: PowerPoolRequest, signal?: AbortSignal) =>
    http.post<PowerPoolPreview>(`${B}/preview`, body, signal),
  addTask: (body: PowerPoolRequest) => http.post<Schemas['AddedTask']>(`${B}/tasks`, body),
}
