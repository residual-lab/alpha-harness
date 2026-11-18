/** Paste Alphas: an LLM's output read back into simulations, and their results copied out. */

import type { components } from '@/api/generated'
import { http } from '@/api/http'

type Schemas = components['schemas']

export type PastePlan = Schemas['PastePlan']
export type PastedAlpha = Schemas['PastedAlpha']

export interface PasteRequest {
  text: string
  cores: number
}

const B = '/api/tools/paste-alphas'

export const pasteAlphas = {
  /** Free: reads the text, simulates nothing. */
  preview: (body: PasteRequest, signal?: AbortSignal) =>
    http.post<PastePlan>(`${B}/preview`, body, signal),
  addTask: (body: PasteRequest) => http.post<Schemas['AddedTask']>(`${B}/tasks`, body),
  copy: (taskId: number) => http.post<Schemas['PasteResults']>(`${B}/tasks/${taskId}/copy`, {}),
}
