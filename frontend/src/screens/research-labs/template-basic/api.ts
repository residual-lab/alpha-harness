/** Template Lab: the account's blocks, templates, previews and tasks. Request bodies are snake_case. */

import type { components } from '@/api/generated'
import { http, qs } from '@/api/http'
import type { SearchLabRequest } from '@/screens/research-labs/search/api'
import type { BlockInfo, TemplateDoc } from '@/screens/research-labs/template-basic/tree'

type Schemas = components['schemas']

export type TemplateLabOptions = Omit<Schemas['BasicTemplateOptions'], 'blocks'> & {
  blocks: BlockInfo[]
}
export type TemplateSummary = Omit<Schemas['BasicTemplateSummary'], 'tree'> & { tree: TemplateDoc }
export type TemplateLabPreview = Schemas['BasicTemplatePreview']

export interface TemplateLabRequest extends SearchLabRequest {
  tree: TemplateDoc
  template_name: string
}

export interface TemplateBody {
  name: string
  description?: string | null
  tree: TemplateDoc
}

const B = '/api/template-basic-lab'

export const templateLab = {
  /** `refresh` syncs the account's operators from BRAIN first. */
  options: (refresh = false) =>
    http.get<TemplateLabOptions>(`${B}/options${qs({ refresh: refresh || undefined })}`),
  templates: () => http.get<{ templates: TemplateSummary[] }>(`${B}/templates`),
  create: (body: TemplateBody) => http.post<TemplateSummary>(`${B}/templates`, body),
  update: (id: number, body: TemplateBody) =>
    http.put<TemplateSummary>(`${B}/templates/${id}`, body),
  remove: (id: number) => http.del<Schemas['BasicTemplateRemoved']>(`${B}/templates/${id}`),
  /** Free; queues nothing. */
  preview: (body: TemplateLabRequest) => http.post<TemplateLabPreview>(`${B}/preview`, body),
  /** Adds the template's search to Tasks, not started. Spends nothing until it is run there. */
  addTask: (body: TemplateLabRequest & { simulations: number }) =>
    http.post<Schemas['AddedTask']>(`${B}/tasks`, body),
}
