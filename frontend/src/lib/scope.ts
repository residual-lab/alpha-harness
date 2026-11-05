/**
 * The market a screen is pointed at: instrument type, region, delay and universe, kept per
 * screen and persisted so a screen reopens where it was left. What is legal in a region comes
 * from the platform's own settings schema, never a hardcoded table — a simulation with an
 * illegal universe still spends quota.
 */

import { useQuery } from '@tanstack/react-query'
import { useCallback, useMemo } from 'react'
import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { auth } from '@/api/core'
import { INSTRUMENT_TYPE, RA_REGIONS, REGION_AGNOSTIC_REGION } from '@/api/platform'
import type { Scope } from '@/api/types'
import { followOtherTabs } from '@/lib/tabs'

export const DEFAULT_SCOPE: Scope = {
  instrumentType: INSTRUMENT_TYPE,
  region: 'USA',
  delay: 1,
  universe: 'TOP3000',
}

/**
 * Whether this is the region that means every region at once. Choosing it makes the
 * simulation region-agnostic: one run across USA, Europe, Asia and Global, submittable when
 * it works in two or more of them. BRAIN offers it under no other simulation type.
 */
export const isRegionAgnostic = (scope: { region: string }): boolean =>
  scope.region === REGION_AGNOSTIC_REGION

/**
 * Whether a region-agnostic simulation can be translated into this market. Nowhere else can
 * one run, so asking whether a field exists region-agnostically is a question only these
 * four markets can act on.
 */
export const runsRegionAgnostic = (scope: { region: string }): boolean =>
  (RA_REGIONS as readonly string[]).includes(scope.region)

/** BRAIN labels the region-agnostic market `ALL`, which says nothing about what it does. */
export const regionLabel = (region: string): string =>
  region === REGION_AGNOSTIC_REGION ? 'All Regions' : region

/** One market as a single string, for sets and select values. */
export const marketKey = (m: { region: string; delay: number; universe: string }): string =>
  `${m.region}|${m.delay}|${m.universe}`

/** A stored scope, or the default when there is none. */
const usable = (scope: Scope | undefined): Scope => scope ?? DEFAULT_SCOPE

interface ScopeStore {
  scopes: Record<string, Scope>
  set: (key: string, scope: Scope) => void
}

const useScopeStore = create<ScopeStore>()(
  persist(
    (set) => ({
      scopes: {},
      set: (key, scope) => set((state) => ({ scopes: { ...state.scopes, [key]: scope } })),
    }),
    { name: 'alpha-harness-scope' },
  ),
)
followOtherTabs(useScopeStore)

/** One screen's scope (`key` = route or lab id) and a setter that merges changes. */
export function useScope(key: string): [Scope, (change: Partial<Scope>) => void] {
  const scope = useScopeStore((state) => usable(state.scopes[key]))
  const set = useScopeStore((state) => state.set)
  const update = useCallback(
    (change: Partial<Scope>) =>
      set(key, { ...usable(useScopeStore.getState().scopes[key]), ...change }),
    [key, set],
  )
  return [scope, update]
}

export interface Choice<V extends string = string> {
  value: V
  label: string
}

export interface ScopeOptions {
  regions: Choice[]
  delays: Choice[]
  universes: Choice[]
  neutralizations: Choice[]
  /** False until the platform has answered for this region and delay. */
  ready: boolean
  isError: boolean
}

const choices = (list: { value: string | number; label: string }[] | null | undefined): Choice[] =>
  (list ?? []).map((c) => ({
    value: String(c.value),
    label: String(c.label ?? c.value),
  }))

/** Legal regions, delays, universes and neutralizations for a scope, from BRAIN's schema. */
export function useScopeOptions(scope: Scope): ScopeOptions {
  const query = useQuery({
    queryKey: ['scope-options', scope.instrumentType, scope.region, scope.delay],
    queryFn: () =>
      auth.settingsOptions({
        instrumentType: scope.instrumentType,
        region: scope.region,
        delay: scope.delay,
      }),
    staleTime: 10 * 60 * 1000,
  })
  // Memoised because `ScopePicker` keeps these arrays in effect dependencies. Rebuilt on
  // every render they would re-run those effects on every render of every screen that shows
  // a market picker — harmless today, but only because the effects happen to be idempotent.
  const fields = query.data?.fields
  return useMemo(() => {
    const universes = choices(fields?.['universe']?.choices)
    return {
      // BRAIN's own labels, `ALL` included. The picker is a list of region codes in a
      // monospace control, and "All Regions" among USA, GLB and EUR reads as a different
      // kind of thing and stretches the menu to fit it. `regionLabel` spells it out where
      // there is prose to spell it out in.
      regions: choices(fields?.['region']?.choices),
      delays: choices(fields?.['delay']?.choices),
      universes,
      neutralizations: choices(fields?.['neutralization']?.choices),
      ready: universes.length > 0,
      isError: query.isError,
    }
  }, [fields, query.isError])
}
