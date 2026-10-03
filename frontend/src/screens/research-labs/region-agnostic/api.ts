/** Region Agnostic Lab: region-agnostic fields, the regions to run them in, then a task. */

import type { components } from '@/api/generated'
import { http, qs } from '@/api/http'

type Schemas = components['schemas']

export type RegionAgnosticOptions = Schemas['RegionAgnosticOptions']
export type RegionAgnosticPreview = Schemas['RegionAgnosticPreview']

export interface RegionAgnosticRequest {
  delay: number
  /** The All Regions market's universe the fields were chosen in. */
  universe: string
  regions: string[]
  /** BRAIN's region-agnostic universe size: LARGE, MEDIUM or SMALL. */
  size: 'LARGE' | 'MEDIUM' | 'SMALL'
  field_ids: string[]
  dataset_ids: string[]
  rank_by: string | null
  model: string | null
  prompt_id: number | null
  /** Empty draws from every neutralization the chosen regions share. */
  neutralizations: string[]
  cores: number
  simulations: number
}

const B = '/api/region-agnostic-lab'

export const regionAgnosticLab = {
  options: (delay: number) => http.get<RegionAgnosticOptions>(`${B}/options${qs({ delay })}`),
  /** Free: no LLM call, no simulation. */
  preview: (body: RegionAgnosticRequest) => http.post<RegionAgnosticPreview>(`${B}/preview`, body),
  addTask: (body: RegionAgnosticRequest) => http.post<Schemas['AddedTask']>(`${B}/tasks`, body),
}
