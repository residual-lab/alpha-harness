/** Fields Sampler: one Alpha's data fields swapped for others, its settings held. */

import type { components } from '@/api/generated'
import { http } from '@/api/http'

type Schemas = components['schemas']

export type FieldsPlan = Schemas['FieldsPlan']
export type FieldSlot = Schemas['FieldSlot']
export type ProvenSettings = Schemas['ProvenSettings']
export type SamplerMode = 'sweep' | 'similar' | 'buckets'

export interface FieldsRequest {
  alphaId: string
  mode: SamplerMode
  k: number
  /** Empty swaps every field. */
  fields: string[]
  /** Other Alphas of the same expression whose settings every swap also runs at. */
  alphas: string[]
  /** Every simulation the task may run; null lets the server allow twice the first sweep. */
  budget: number | null
  cores: number
}

const B = '/api/tools/fields-sampler'

export const fieldsSampler = {
  /** Free: reads the Alpha and the catalog, simulates nothing. */
  preview: (body: FieldsRequest) => http.post<FieldsPlan>(`${B}/preview`, body),
  addTask: (body: FieldsRequest) => http.post<Schemas['AddedTask']>(`${B}/tasks`, body),
}
