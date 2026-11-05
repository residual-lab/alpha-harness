/** What the labs share around a task: its draft, market and datasets, and its preview. */

import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { useNavigate } from '@tanstack/react-router'
import { useEffect } from 'react'
import type { Scope } from '@/api/types'
import { DEFAULT_SCOPE, useScope } from '@/lib/scope'
import { useDebounced } from '@/lib/use-debounced'
import { type PickFrom, useDatasetPick } from '@/screens/data/dataset-pick'
import { type FieldFilterState, startDatasetPick } from '@/screens/data/state'

export interface LabDraft {
  region: string
  delay: number
  universe: string
  datasetIds: string[]
  /** The Data Explorer's filter the datasets were chosen under: the lab uses only the fields it
   *  shows. `null` uses every field in them; a draft saved before labs kept one has none. */
  fieldFilter: FieldFilterState | null
  /** `null` until chosen in the form: until then Settings' default applies. */
  cores: number | null
  /** `null` until the user assigns them: a task always has simulations chosen on purpose. */
  simulations: number | null
  decay: number
  /** `null` until the user chooses: then the lab allows `vec_avg`. */
  vectorOperators: string[] | null
  /** Empty leaves the lab on its own four group neutralizations. */
  neutralizations: string[]
}

export const LAB_DEFAULTS: LabDraft = {
  region: DEFAULT_SCOPE.region,
  delay: DEFAULT_SCOPE.delay,
  universe: DEFAULT_SCOPE.universe,
  datasetIds: [],
  fieldFilter: null,
  cores: null,
  simulations: null,
  decay: 0,
  vectorOperators: null,
  neutralizations: [],
}

export const MAX_SIMULATIONS = 100_000

/** A draft's market and datasets, and the round trip to the Data Explorer to choose them. */
type LabMarket = Pick<LabDraft, 'region' | 'delay' | 'universe' | 'datasetIds' | 'fieldFilter'>

export function useLabMarket(
  draft: LabMarket,
  set: (change: Partial<LabMarket>) => void,
  from: PickFrom,
) {
  const navigate = useNavigate()
  const [, setDataScope] = useScope('data')
  const scope: Scope = {
    instrumentType: 'EQUITY',
    region: draft.region,
    delay: draft.delay,
    universe: draft.universe,
  }
  const chosen = draft.datasetIds.length > 0

  // Back from the Data Explorer with a finished pick for this lab.
  useEffect(() => {
    const pick = useDatasetPick.getState().take(from)
    if (pick)
      set({
        region: pick.scope.region,
        delay: pick.scope.delay,
        universe: pick.scope.universe,
        datasetIds: pick.ids,
        fieldFilter: pick.extra ?? null,
      })
  }, [from, set])

  /** Over to the Data Explorer, from `ids` and `filter`: the lab's own unless given. */
  const choose = (ids = draft.datasetIds, filter = draft.fieldFilter) => {
    // On the filter this lab applies, so the Explorer shows the fields it will use.
    startDatasetPick(scope, ids, from, filter)
    setDataScope(scope)
    void navigate({ to: '/data' })
  }
  /** To another market. Datasets belong to a region and delay, so leaving one drops them. */
  const move = (change: Partial<Scope>) => {
    const next: Partial<LabMarket> = {}
    if (change.region !== undefined) next.region = change.region
    if (change.delay !== undefined) next.delay = change.delay
    if (change.universe !== undefined) next.universe = change.universe
    const moved =
      (next.region ?? draft.region) !== draft.region || (next.delay ?? draft.delay) !== draft.delay
    if (moved) Object.assign(next, { datasetIds: [], fieldFilter: null })
    if (Object.keys(next).length) set(next)
  }
  return { chosen, scope, choose, move }
}

/**
 * A lab's free preview of `body`, asked once the form has been still for `wait` ms. `current`
 * is whether the plan answers the form as it is now rather than an earlier state of it.
 */
export function useLabPreview<Body, Plan>(
  lab: string,
  body: Body,
  preview: (body: Body, signal: AbortSignal) => Promise<Plan>,
  { enabled = true, wait = 300 }: { enabled?: boolean; wait?: number } = {},
) {
  const key = JSON.stringify(body)
  const settled = useDebounced(key, wait)
  const query = useQuery({
    queryKey: [lab, 'preview', settled],
    queryFn: ({ signal }) => preview(JSON.parse(settled) as Body, signal),
    enabled: enabled && settled === key,
    placeholderData: keepPreviousData,
  })
  return { preview: query, current: settled === key && !query.isFetching }
}

/** `vec_avg` until the user chooses vector operators. */
export function vectorOperatorsOf(draft: LabDraft, available: string[] | undefined): string[] {
  return draft.vectorOperators ?? (available?.includes('vec_avg') ? ['vec_avg'] : [])
}

/**
 * The chosen values this market offers. One ticked in another market is not shown here, so it
 * must not be sent either; until the market's list loads, the choice is kept as it is.
 */
export const offeredOnly = (chosen: string[], available: readonly { value: string }[]) =>
  available.length ? chosen.filter((v) => available.some((a) => a.value === v)) : chosen

/** The market and settings both labs send to preview a task. */
export function labBody(
  draft: LabDraft,
  vectorOperators: string[],
  cores: number,
  neutralizations: readonly { value: string }[],
) {
  return {
    region: draft.region,
    delay: draft.delay,
    universe: draft.universe,
    dataset_ids: draft.datasetIds,
    field_filter: draft.fieldFilter ?? null,
    vector_operators: vectorOperators,
    neutralizations: offeredOnly(draft.neutralizations, neutralizations),
    decay: draft.decay,
    cores,
  }
}

export function simulationsValid(simulations: number | null, maxSimulations: number): boolean {
  return simulations !== null && simulations >= 1 && simulations <= maxSimulations
}
