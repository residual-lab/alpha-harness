/**
 * The assistant: providers, keys and their daily budgets, and the chat. Its prompts live in
 * `@/screens/prompts`.
 * Bodies are snake_case only. Keys never come back — only `hint`.
 */

import type { components } from '@/api/generated'
import { ApiError, http, qs } from '@/api/http'
import type { ScopeBody } from '@/api/types'
import { fmt } from '@/lib/format'

type Schemas = components['schemas']

export type LLMModel = Schemas['ModelInfo']
export type LLMModels = Schemas['LLMModels']
export type LLMProvider = Schemas['LLMProvider']
export type LLMProvidersResponse = Schemas['LLMProviders']
export type LLMKey = Schemas['LLMKey']
export type LLMKeyStatus = Schemas['LLMKeyStatus']

export interface AddKeyRequest {
  key: string
  label?: string | null
  provider?: string
  /** Daily request ceiling. The backend refuses a paid provider's key without one. */
  daily_limit?: number | null
}

export type OfferedModels = Schemas['OfferedModels']

export interface SetModelRequest {
  provider: string
  model: string
  requests_per_minute: number
  requests_per_day: number
  /** IANA time zone whose midnight starts the provider's new day for this model. */
  reset_timezone: string
  /** Most tokens one Power Pool prompt may use; null for the default. */
  max_prompt_tokens: number | null
}

export type KeyCheck = Schemas['KeyWorks'] | Schemas['KeyFailed']
export type Reasoning = Schemas['ChatOptions']['defaultReasoning']
export type ChatOptions = Schemas['ChatOptions']
export type ChatThreadSummary = Schemas['ChatThreadSummary']

export interface ChatPick {
  field: string
  why: string
  [extra: string]: unknown
}

export type ChatMessage = Omit<Schemas['ChatMessageOut'], 'meta'> & {
  meta: {
    picks?: ChatPick[]
    datasets?: string[]
    dropped?: string[]
    catalogNote?: string
    model?: string
    reasoning?: Reasoning
    tokens?: number
  }
}

export type ChatThread = Omit<Schemas['ChatThreadOut'], 'messages'> & { messages: ChatMessage[] }

export interface ChatSayRequest {
  text: string
  scope: ScopeBody
  thread_id?: number | null
  model?: string | null
  reasoning?: Reasoning
  dataset_ids?: string[]
  /** A saved Assistant prompt; omitted or null sends the built-in. */
  prompt_id?: number | null
}

export type ChatReply = Omit<Schemas['ChatReply'], 'picks'> & { picks: ChatPick[] }

/** A downloaded catalog scope. Raw row, snake_case. */
export interface DownloadedScope {
  instrument_type: string
  region: string
  delay: number
  universe: string
  fields: number
}

export const llm = {
  providers: () => http.get<LLMProvidersResponse>('/api/llm/providers'),
  models: () => http.get<LLMModels>('/api/llm/models'),
  /** Sets a model up, or changes its limits. 400 llm_error when the id is another provider's. */
  setModel: (body: SetModelRequest) => http.put<LLMModel>('/api/llm/models', body),
  /** Query parameters, because model ids carry slashes. */
  removeModel: (provider: string, model: string) =>
    http.del<void>(`/api/llm/models${qs({ provider, model })}`),
  /** Asked live of the provider's first enabled Key. */
  offered: (provider: string) =>
    http.get<OfferedModels>(`/api/llm/providers/${encodeURIComponent(provider)}/models`),
  keys: () => http.get<LLMKeyStatus>('/api/llm/keys'),
  /** 400 llm_error for a duplicate key. */
  addKey: (body: AddKeyRequest) => http.post<LLMKey>('/api/llm/keys', body),
  setEnabled: (id: number, enabled: boolean, dailyLimit?: number | null) =>
    // snake_case, like every other body here: the backend reads `daily_limit` and silently
    // ignores anything else, so a camelCase key would arrive as "no cap given".
    http.put<LLMKey>(`/api/llm/keys/${id}`, {
      enabled,
      daily_limit: dailyLimit ?? undefined,
      clear_daily_limit: dailyLimit === null,
    }),
  removeKey: (id: number) => http.del<void>(`/api/llm/keys/${id}`),
  checkKey: (id: number) => http.post<KeyCheck>(`/api/llm/keys/${id}/check`),
  checkAll: () => http.post<KeyCheck[]>('/api/llm/keys/check'),
}

export const chat = {
  options: () => http.get<ChatOptions>('/api/chat/options'),
  threads: (limit = 30) => http.get<ChatThreadSummary[]>(`/api/chat/threads${qs({ limit })}`),
  /** 404 no_such_thread. */
  thread: (id: number) => http.get<ChatThread>(`/api/chat/threads/${id}`),
  deleteThread: (id: number) => http.del<void>(`/api/chat/threads/${id}`),
  /** Spends one assistant request. An undownloaded scope fails with a plain 500. */
  say: (body: ChatSayRequest) => http.post<ChatReply>('/api/chat', body),
  downloadedScopes: () => http.get<DownloadedScope[]>('/api/catalog/scopes'),
}

/** The extra facts a 429 llm_budget_exhausted carries: when to retry, and what is left. */
export function budgetDetail(error: unknown): string | null {
  if (!(error instanceof ApiError) || error.code !== 'llm_budget_exhausted') return null
  const keys = (error.body['keys'] ?? []) as { dailyRemaining?: number }[]
  const left = keys.reduce((sum, k) => sum + (k.dailyRemaining ?? 0), 0)
  return `Retry in ${fmt.duration(error.body.retryAfter)} · ${fmt.int(left)} requests left today across ${fmt.int(keys.length)} key budgets.`
}
