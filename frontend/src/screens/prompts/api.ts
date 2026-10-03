/**
 * LLM Prompts: the built-in system prompts and the user's saved ones. A built-in has a null
 * `id` and its `kind` is its slug; a saved prompt's `kind` says which built-in it stands in for.
 */

import { useQuery } from '@tanstack/react-query'
import type { components } from '@/api/generated'
import { http } from '@/api/http'

type Schemas = components['schemas']

export type LibraryPrompt = Schemas['LibraryPrompt']
export type PromptLibrary = Schemas['PromptLibrary']
export type PromptKind = Schemas['PromptKind']

export interface NewPrompt {
  name: string
  kind: string
  body?: string
  based_on?: string | null
}

const B = '/api/prompts'

export const LIBRARY_KEY = ['prompts', 'library'] as const

export const prompts = {
  library: () => http.get<PromptLibrary>(B),
  create: (body: NewPrompt) => http.post<LibraryPrompt>(B, body),
  /** Only what is sent changes. */
  edit: (id: number, body: { name?: string; body?: string }) =>
    http.put<LibraryPrompt>(`${B}/${id}`, body),
  remove: (id: number) => http.del<void>(`${B}/${id}`),
}

export const usePromptLibrary = () => useQuery({ queryKey: LIBRARY_KEY, queryFn: prompts.library })

/** A prompt's key in the URL and in a Select: a saved prompt's id, or a built-in's slug. */
export const promptKey = (p: Pick<LibraryPrompt, 'id' | 'kind'>) =>
  p.id === null ? p.kind : String(p.id)

/** The same counts the backend reports, for text that is still being typed. */
export function countText(body: string) {
  const trimmed = body.trim()
  return {
    characters: body.length,
    words: trimmed ? trimmed.split(/\s+/).length : 0,
    // `llm.text.estimate_tokens`: four characters a token.
    estimatedTokens: Math.max(1, Math.floor(body.length / 4)),
  }
}
