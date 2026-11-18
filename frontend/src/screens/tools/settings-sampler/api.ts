/** Settings Sampler: where one proven expression could also run, and queueing it there. */

import type { components } from '@/api/generated'
import { http } from '@/api/http'

type Schemas = components['schemas']

export type SettingsPlan = Schemas['SettingsPlan']
export type Pair = Schemas['Pair']

export interface MarketPick {
  region: string
  delay: number
  universe: string
}

/** How every simulation in the sweep is held, whatever market it lands in. Each is optional:
 * left out, an Alpha's own value stands, or the platform default when there is no Alpha. */
export interface Holding {
  decay?: number
  truncation?: number
  pasteurization?: 'ON' | 'OFF'
  nanHandling?: 'ON' | 'OFF'
  /** `P0Y0M0D` to `P6Y0M0D`, BRAIN's own bounds. */
  testPeriod?: string
}

/** Where the sweep's expression comes from: an Alpha, or an expression typed in. */
export type Source = ({ alphaId: string } | { expression: string }) & Holding

export type SampleRequest = Source & {
  /** Empty means every market the plan offers; likewise for each filter below. */
  markets: MarketPick[]
  neutralizations: string[]
  pairs: Pair[]
  /** Concurrent slots the task holds; ten simulations ride in each. */
  cores: number
  /** Drop NONE neutralization with no investability constraint: it is not market neutral. */
  marketNeutralOnly?: boolean
  /** Truncation set per market by the Truncation Agent instead of held at one value. */
  truncationAgent?: boolean
  /** Every simulation run at each of these instead of at the held `decay`. */
  decays?: number[]
  /** Off unless turned on, whatever the Alpha ran with: it slows every simulation. */
  visualization?: boolean
}

const B = '/api/tools/settings-sampler'

export const settingsSampler = {
  /** Free: reads the Alpha (if any) and the catalog, simulates nothing. */
  preview: (source: Source) => http.post<SettingsPlan>(`${B}/preview`, source),
  addTask: (body: SampleRequest) => http.post<Schemas['AddedTask']>(`${B}/tasks`, body),
}

/** How an Alpha is held to its instruments' liquidity. BRAIN refuses both ON, so the two
 *  settings are one three-way choice; `None` matches the Investability column on the tables. */
export const pairLabel = (p: Pair) =>
  p.maxTrade === 'OFF' && p.maxPosition === 'OFF'
    ? 'None'
    : p.maxTrade === 'ON'
      ? 'Max Trade'
      : 'Max Position'
