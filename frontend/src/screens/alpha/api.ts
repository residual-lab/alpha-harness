/**
 * The Alpha page's contract. Correlations and the performance comparison spend BRAIN's
 * hourly budgets, so they run on a click and come back cached with `fetchedAt`; only
 * `refresh` asks BRAIN again. Nothing here submits an Alpha.
 */

import type { components } from '@/api/generated'
import { ApiError, http, qs } from '@/api/http'
import type { AlphaCheck } from '@/api/types'
import type { BrainCorrelation } from '@/screens/pool/api'

type Schemas = components['schemas']

/** One period's block as BRAIN sends it: its figures, and on IS and OS its checks. */
export interface PeriodBlock {
  startDate?: string
  checks?: AlphaCheck[]
  riskNeutralized?: Record<string, unknown>
  investabilityConstrained?: Record<string, unknown>
  [figure: string]: unknown
}

export type AlphaInfo = Omit<Schemas['AlphaInfo'], 'periods'> & {
  periods: Partial<Record<'train' | 'test' | 'is' | 'os' | 'prod', PeriodBlock>>
}
export type AlphaView = Omit<Schemas['AlphaView'], 'alpha'> & { alpha: AlphaInfo }
export type AlphaProperties = Schemas['AlphaProperties']
export type AlphaDescription = Schemas['AlphaDescription']

export type CorrelationKind = 'self' | 'power-pool' | 'prod'

/** One of BRAIN's series: a schema naming each column, then rows in that order. */
export interface Recordset {
  schema?: {
    name?: string
    title?: string
    properties: { name: string; title?: string; type?: string }[]
  }
  records?: unknown[][]
  /** Set when BRAIN could not be asked again and an older copy stands in. */
  problem?: string
}

export type RecordsetName = 'pnl' | 'sharpe' | 'turnover' | 'yearly-stats'

/** `cached: false` only answers a `cached` read that found nothing kept. */
export type Kept<T> = (T & { cached: true; fetchedAt: string }) | { cached: false }

export type Correlation = Kept<BrainCorrelation>

/** `cached` reads only what is kept; `run` asks BRAIN if nothing is; `refresh` always asks. */
export type Read = 'cached' | 'run' | 'refresh'

const read = (mode: Read) =>
  qs({ cached_only: mode === 'cached' || null, refresh: mode === 'refresh' || null })

type Figures = Record<string, number | null | undefined>

/** The pool before and after the Alpha joins its partition, as BRAIN reports it. */
export type Performance = Kept<{
  partition?: string[]
  stats?: { before: Figures | null; after: Figures | null }
  yearlyStats?: { before: Recordset | null; after: Recordset | null }
  pnl?: Recordset
  [score: string]: unknown
}>

const id = (alphaId: string) => encodeURIComponent(alphaId)

/** BRAIN's answer, 410 or 412, when a correlation does not apply to this Alpha. */
export const notApplicable = (error: unknown) =>
  error instanceof ApiError && [410, 412].includes(Number(error.body['platformStatus']))

export const alpha = {
  page: (alphaId: string) => http.get<AlphaView>(`/api/alphas/${id(alphaId)}/page`),
  recordset: (alphaId: string, name: RecordsetName) =>
    http.get<Recordset>(`/api/alphas/${id(alphaId)}/recordsets/${name}`),
  save: (alphaId: string, body: AlphaProperties) =>
    http.patch<AlphaInfo>(`/api/alphas/${id(alphaId)}`, body),
  /** A Power Pool description in BRAIN's template, by the model chosen in Settings. Not saved. */
  describe: (alphaId: string) =>
    http.post<AlphaDescription>(`/api/alphas/${id(alphaId)}/description`),
  correlation: (alphaId: string, kind: CorrelationKind, mode: Read) =>
    http.get<Correlation>(`/api/alphas/${id(alphaId)}/correlations/${kind}${read(mode)}`),
  performance: (alphaId: string, mode: Read) =>
    http.get<Performance>(`/api/alphas/${id(alphaId)}/performance${read(mode)}`),
}
