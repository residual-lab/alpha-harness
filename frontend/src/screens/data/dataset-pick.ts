/** Picking datasets, or single fields of them, in the Data Explorer's Fields table for a lab. */

import { createPick } from '@/lib/pick'
import type { FieldFilterState } from '@/screens/data/state'

export type PickFrom =
  | '/labs/search'
  | '/labs/template'
  | '/labs/template/basic'
  | '/labs/power-pool'
  | '/labs/region-agnostic'

/** A field ticked in the Fields table, with the dataset it belongs to. */
export interface PickedField {
  id: string
  dataset: string
  /** MATRIX, VECTOR or GROUP; absent from a field chosen before it was recorded. */
  type?: string | null
}

/** Ticked fields, ranked by the table's order when the pick was finished. */
export interface FieldPick {
  fields: PickedField[]
  /** How they were ranked, as a lab's prompt says it: "Alphas, most first". */
  rankBy: string | null
}

/**
 * What a pick carries back besides its dataset ids: the filter they were chosen under, which
 * the lab applies to their fields, or the single fields ticked instead, ranked. Fields win:
 * with some, the filter is `null` because the fields are already exactly what was wanted.
 */
export interface DatasetPickExtra extends FieldPick {
  filter: FieldFilterState | null
}

export const useDatasetPick = createPick<PickFrom, DatasetPickExtra>(
  'alpha-harness-dataset-pick',
  '/labs/search',
)
