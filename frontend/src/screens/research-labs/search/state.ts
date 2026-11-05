/** The Search Lab's draft, apart from its screen so other screens can hand it a market. */

import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { followOtherTabs } from '@/lib/tabs'
import { LAB_DEFAULTS, type LabDraft } from '@/screens/research-labs/lab-task'

/** The Search Lab's choices, kept between visits. */
export const useSearchLab = create<LabDraft>()(
  persist(() => LAB_DEFAULTS, {
    name: 'alpha-harness-search-lab',
    // Version 1 leaves cores unchosen, so Settings' default for new tasks applies.
    version: 1,
    migrate: (stored) => ({ ...(stored as LabDraft), cores: null }),
  }),
)
followOtherTabs(useSearchLab)
