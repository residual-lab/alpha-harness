/** Template Lab: the account's options, saved templates, previews and tasks. Request bodies are snake_case. */

import type { components } from '@/api/generated'
import { http, qs } from '@/api/http'
import type { FieldFilterState } from '@/screens/data/state'

type Schemas = components['schemas']

/** Fields of the chosen datasets, narrowed by the Data Explorer's filter. */
export interface FieldsVariable {
  kind: 'fields'
  dataset_ids: string[]
  /** Region Exclusive, field types and anything else the Data Explorer narrows on. */
  filter?: FieldFilterState | null | undefined
  /** With matrix and vector fields both searched: what a vector field is reduced with. */
  vector_operators?: string[] | undefined
}

/** A typed list: numbers, groups, operator names or whole expressions. */
export interface ValuesVariable {
  kind: 'values'
  values: string
}

export type VariableDef = FieldsVariable | ValuesVariable

export type Investability = 'none' | 'max_trade' | 'max_position'

export const INVESTABILITY_LABELS: Record<Investability, string> = {
  none: 'None',
  max_trade: 'Max Trade',
  max_position: 'Max Position',
}

export type TemplateLabOptions = Schemas['TemplateLabOptions']
export type TemplateLabPreview = Schemas['TemplateLabPreview']
export type VariableInfo = Schemas['VariableInfo']
export type TemplateStats = Schemas['TemplateStats']
export type TreeNode = Schemas['TreeNode']
export type TemplateSummary = Omit<Schemas['TemplateSummary'], 'variables'> & {
  variables: Record<string, VariableDef>
}

export interface TemplateLabRequest {
  region: string
  delay: number
  template: string
  variables: Record<string, VariableDef>
  universes: string[]
  neutralizations: string[]
  investability: Investability[]
  decay: number
  truncation: number
  pasteurization: 'ON' | 'OFF'
  nan_handling: 'ON' | 'OFF'
  test_period: string
  cores: number
  template_name: string
}

/** A template and what each of its variables stands for, to be sized. */
export interface Sizing {
  text: string
  variables: Record<string, VariableDef>
}

export interface TemplateBody {
  name: string
  description?: string | null
  text: string
  variables: Record<string, VariableDef>
}

const B = '/api/template-lab'

export const templateLab = {
  /** `refresh` syncs the account's operators from BRAIN first. */
  options: (refresh = false) =>
    http.get<TemplateLabOptions>(`${B}/options${qs({ refresh: refresh || undefined })}`),
  templates: () => http.get<{ templates: TemplateSummary[] }>(`${B}/templates`),
  create: (body: TemplateBody) => http.post<TemplateSummary>(`${B}/templates`, body),
  update: (id: number, body: TemplateBody) =>
    http.put<TemplateSummary>(`${B}/templates/${id}`, body),
  remove: (id: number) => http.del<Schemas['TemplateRemoved']>(`${B}/templates/${id}`),
  /** Each template's operators and fields, and the kinds of Alpha it makes. Free. */
  stats: (body: { region: string; delay: number; templates: Sizing[] }) =>
    http.post<{ stats: TemplateStats[] }>(`${B}/stats`, body),
  /** The template as the Blocks view draws it, or why it can't be read. */
  tree: (text: string, signal?: AbortSignal) =>
    http.post<Schemas['TemplateTree']>(`${B}/tree`, { text }, signal),
  /** Free; queues nothing. */
  preview: (body: TemplateLabRequest, signal?: AbortSignal) =>
    http.post<TemplateLabPreview>(`${B}/preview`, body, signal),
  /** Adds the template's search to Tasks, not started. Spends nothing until it is run there. */
  addTask: (body: TemplateLabRequest & { simulations: number }) =>
    http.post<Schemas['AddedTask']>(`${B}/tasks`, body),
}
