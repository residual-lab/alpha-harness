/**
 * One row shape for every Alpha table, whatever screen it is on. Each source the app reads
 * Alphas from is turned into it here, so the table, its columns and its sorting are written
 * once. A field a source does not have is left out, and its column shows a dash.
 */

import type { AlphaRow } from '@/screens/pool/api'
import type { PortfolioMember } from '@/screens/portfolio/api'
import type { RankedAlpha, TaskAlpha } from '@/screens/tasks/api'

/** How an Alpha is held to its instruments' liquidity: BRAIN refuses Max Trade and Max
 *  Position both on, so the two settings are one three-way choice. */
export type Investability = 'max_trade' | 'max_position' | 'none'

export interface AlphaTableRow {
  /** Unique in its table: the Alpha's id, or the trial's while it has none. */
  key: string
  alphaId: string | null
  name?: string | null
  expression: string | null
  status?: string | null
  dateCreated?: string | null
  dateSubmitted?: string | null
  operatorCount?: number | null
  classifications?: string[]
  tags?: string[]
  pyramids?: string[]
  categories?: string[]
  /** The region-agnostic parent a submitted child went in under. */
  raParent?: string | null

  region: string | null
  /** A region-agnostic Alpha's figures are one region's: that one. */
  standingRegion?: string | null
  delay: number | null
  universe: string | null
  neutralization: string | null
  decay: number | null
  truncation: number | null
  investability: Investability | null

  sharpe: number | null
  turnover: number | null
  fitness: number | null
  returns: number | null
  drawdown: number | null
  margin: number | null
  trainSharpe?: number | null
  testSharpe?: number | null
  testFitness?: number | null
  longCount?: number | null
  shortCount?: number | null
  kRatio?: number | null
  incrementalSharpe?: number | null
  /** At the 5 bps a task charges, from its own download. */
  afterCostSharpe?: number | null

  prodCorrelation?: number | null
  selfCorrelation?: number | null
  powerPoolCorrelation?: number | null
  portfolioCorrelation?: number | null
  maxPairwiseCorrelation?: number | null

  /** A task's own: what it searches for, the trial, what refused it, which task. */
  value?: number | null
  trial?: number
  failedChecks?: string[]
  refusedBy?: string[]
  quick?: boolean
  submittable?: boolean
  pending?: boolean
  submitted?: boolean
  taskName?: string | null
  /** Whether its daily PnL is stored on this computer. */
  hasPnl?: boolean
  /** The limits its own checks set its figures, by check name. */
  limits?: Partial<Record<string, number>>
}

const investabilityOf = (maxTrade: unknown, maxPosition: unknown): Investability =>
  maxTrade === 'ON' ? 'max_trade' : maxPosition === 'ON' ? 'max_position' : 'none'

const num = (value: unknown): number | null =>
  typeof value === 'number' ? value : value == null || value === '' ? null : Number(value)
const str = (value: unknown): string | null => (value == null ? null : String(value))

export function fromMember(m: PortfolioMember): AlphaTableRow {
  return {
    ...m,
    key: m.alphaId,
    expression: m.expression ?? null,
    neutralization: m.neutralization ?? null,
    decay: m.decay ?? null,
    truncation: m.truncation ?? null,
  }
}

export function fromRanked(r: RankedAlpha | TaskAlpha): AlphaTableRow {
  const s = r.settings ?? {}
  return {
    ...r,
    key: String(r.trialId),
    trial: r.number,
    region: str(s['region']),
    delay: num(s['delay']),
    universe: str(s['universe']),
    neutralization: str(s['neutralization']),
    decay: num(s['decay']),
    truncation: num(s['truncation']),
    investability: investabilityOf(s['maxTrade'], s['maxPosition']),
    taskName: 'taskName' in r ? r.taskName : null,
  }
}

export function fromStored(r: AlphaRow): AlphaTableRow {
  return {
    ...r,
    key: r.alphaId,
    investability: investabilityOf(r.maxTrade, r.maxPosition),
  }
}
