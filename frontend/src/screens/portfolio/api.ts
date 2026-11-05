/**
 * Portfolio: submitted Alphas combined at equal weight, as BRAIN combines its own pool.
 * Request bodies are snake_case ONLY — camelCase keys are silently dropped.
 */

import type { components } from '@/api/generated'
import { http } from '@/api/http'

type Schemas = components['schemas']

export type PortfolioMember = Schemas['PortfolioMember']
export type PortfolioResult = Schemas['PortfolioResult']
export type PortfolioStats = Schemas['PortfolioStats']
export type Windows = Schemas['Windows']
export type Investability = PortfolioMember['investability']

export const portfolio = {
  /** With ``costBps``, each Alpha's figures and correlations net of that cost, worked out
   *  locally. */
  members: (costBps?: number) =>
    http.get<Schemas['PortfolioMembers']>(
      costBps ? `/api/portfolio/members?costBps=${costBps}` : '/api/portfolio/members',
    ),
  /** Refresh the submitted Alphas and download any missing PnL and turnover. */
  sync: () => http.post<Schemas['PortfolioSyncStarted']>('/api/portfolio/sync'),
  /** These Alphas as BRAIN shows them, then a glossary of the data fields they read. */
  markdown: (alphaIds: string[]) =>
    http.post<Schemas['CopiedAlphas']>('/api/portfolio/markdown', { alpha_ids: alphaIds }),
  compute: (alphaIds: string[], costBps: number) =>
    http.post<PortfolioResult>('/api/portfolio/compute', {
      alpha_ids: alphaIds,
      cost_bps: costBps,
    }),
}
