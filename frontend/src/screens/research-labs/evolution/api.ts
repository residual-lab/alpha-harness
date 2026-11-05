/**
 * Evolution Lab: choose seed Alphas, cores and simulations, then add the breeding to Tasks,
 * where it runs. Request bodies are snake_case.
 */

import type { components } from '@/api/generated'
import { http } from '@/api/http'

type Schemas = components['schemas']

export type EvolutionOptions = Schemas['EvolutionOptions']
export type SeedRow = Schemas['SeedRow']
export type SeedReason = Schemas['SeedReason']
export type EvolutionPreview = Schemas['EvolutionPreview']
export type AutoSeedsJob = Schemas['AutoSeedsJob']

export interface EvolutionRequest {
  region: string
  delay: number
  universe: string
  alpha_ids: string[]
  /** Empty keeps the lab's default four. */
  neutralizations: string[]
  cores: number
  /** `null`: sized from the simulations. */
  population: number | null
  mutation_rate: number
  /** Needed to add a task; a preview uses it to size the population. */
  simulations?: number
}

const B = '/api/evolution-lab'

export const evolutionLab = {
  /** The markets holding Alphas, and what a task can be set to. */
  options: () => http.get<EvolutionOptions>(`${B}/options`),
  /** Free; queues nothing. */
  preview: (body: EvolutionRequest, signal?: AbortSignal) =>
    http.post<EvolutionPreview>(`${B}/preview`, body, signal),
  /** Chooses seeds in the background. Downloads daily PnL where missing; never simulates. */
  autoSeeds: (body: { region: string; delay: number; universe: string; count: number }) =>
    http.post<Schemas['AutoSeedsStarted']>(`${B}/seeds/auto`, body),
  autoSeedsJob: (jobId: string) =>
    http.get<AutoSeedsJob>(`${B}/seeds/auto/${encodeURIComponent(jobId)}`),
  /** Adds the breeding to Tasks, not started. Spends nothing until it is run there. */
  addTask: (body: EvolutionRequest & { simulations: number }) =>
    http.post<Schemas['AddedTask']>(`${B}/tasks`, body),
}
