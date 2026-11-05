/** The Evolution Lab's choices, kept between visits. */

import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { DEFAULT_SCOPE } from '@/lib/scope'
import { followOtherTabs } from '@/lib/tabs'

export interface EvolutionDraft {
  region: string
  delay: number
  universe: string
  seedIds: string[]
  /** `null` until chosen in the form: until then Settings' default applies. */
  cores: number | null
  /** `null` until the user assigns them: a task always has simulations chosen on purpose. */
  simulations: number | null
  /** `null`: sized from the simulations. */
  population: number | null
  mutationRate: number
  /** Empty leaves the lab on its own four group neutralizations. */
  neutralizations: string[]
  /** The Auto Select job whose result is shown, and the one whose seeds were already taken. */
  autoJobId: string | null
  appliedJobId: string | null
}

export const useEvolutionLab = create<EvolutionDraft>()(
  persist(
    (): EvolutionDraft => ({
      region: DEFAULT_SCOPE.region,
      delay: DEFAULT_SCOPE.delay,
      universe: DEFAULT_SCOPE.universe,
      seedIds: [],
      cores: null,
      simulations: null,
      population: null,
      neutralizations: [],
      mutationRate: 0.05,
      autoJobId: null,
      appliedJobId: null,
    }),
    {
      name: 'alpha-harness-evolution-lab',
      // Version 1 leaves cores unchosen, so Settings' default for new tasks applies.
      version: 1,
      migrate: (stored) => ({ ...(stored as EvolutionDraft), cores: null }),
    },
  ),
)
followOtherTabs(useEvolutionLab)
