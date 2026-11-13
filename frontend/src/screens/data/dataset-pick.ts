/** Picking datasets in the Data Explorer's Fields filters for a lab. */

import { create } from 'zustand'
import { createJSONStorage, persist } from 'zustand/middleware'
import { createPick } from '@/lib/pick'
import type { FieldFilterState } from '@/screens/data/state'

export type PickFrom = '/labs/search' | '/labs/template' | '/labs/power-pool'

/** The ids are the datasets; the extra is the filter they were chosen under, which the lab
 *  applies to their fields. */
export const useDatasetPick = createPick<PickFrom, FieldFilterState | null>(
  'alpha-harness-dataset-pick',
  '/labs/search',
)

/** Fields ticked one by one during that pick, each with its dataset: a lab searches only its
 *  datasets, so Done brings each ticked field's along. Kept across a reload, as the pick is. */
export const useFieldPick = create<{ fields: Record<string, string> }>()(
  persist(() => ({ fields: {} }), {
    name: 'alpha-harness-field-pick',
    storage: createJSONStorage(() => sessionStorage),
  }),
)
