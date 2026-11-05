/**
 * Picking on another screen for a lab, then going back to it: while `active`, that screen turns
 * into a picker and Done leaves the choice in `result` for the lab that started it to take.
 * Kept in sessionStorage, so a reload in the middle of a pick keeps it.
 */

import { create } from 'zustand'
import { createJSONStorage, persist } from 'zustand/middleware'
import type { Scope } from '@/api/types'

export interface PickResult<From, Extra = never> {
  scope: Scope
  ids: string[]
  from: From
  /** Whatever else the picking screen hands back with the ids. */
  extra?: Extra | undefined
}

interface Pick<From, Extra> {
  active: boolean
  scope: Scope | null
  ids: string[]
  /** What was ticked in each region and delay this pick has been to, so going back keeps it. */
  byMarket: Record<string, string[]>
  from: From
  result: PickResult<From, Extra> | null
  start: (scope: Scope, ids: string[], from: From) => void
  finish: (extra?: Extra) => void
  cancel: () => void
  /**
   * Moves the pick to `scope`. What was picked belongs to a region and delay, so another one
   * starts from what was ticked there before, if anything.
   */
  follow: (scope: Scope) => void
  /** The finished pick, once, and only for the lab that started it. */
  take: (from: From) => PickResult<From, Extra> | null
}

/** `first` also stands in for a pick kept from before picks named their lab. */
export function createPick<From extends string, Extra = never>(name: string, first: From) {
  return create<Pick<From, Extra>>()(
    persist(
      (set, get) => ({
        active: false,
        scope: null,
        ids: [],
        byMarket: {},
        from: first,
        result: null,
        start: (scope, ids, from) =>
          set({ active: true, scope, ids, byMarket: {}, from, result: null }),
        finish: (extra) => {
          const { scope, ids, from } = get()
          set({
            active: false,
            scope: null,
            ids: [],
            byMarket: {},
            result: scope ? { scope, ids, from, extra } : null,
          })
        },
        cancel: () => set({ active: false, scope: null, ids: [], byMarket: {}, result: null }),
        follow: (scope) => {
          const { scope: current, ids, byMarket } = get()
          const market = `${scope.region}|${scope.delay}`
          const left = current && `${current.region}|${current.delay}`
          if (left === market) {
            set({ scope })
            return
          }
          const kept = left ? { ...byMarket, [left]: ids } : byMarket
          set({ scope, ids: kept[market] ?? [], byMarket: kept })
        },
        take: (from) => {
          const result = get().result
          if (!result || (result.from ?? first) !== from) return null
          set({ result: null })
          return result
        },
      }),
      { name, storage: createJSONStorage(() => sessionStorage) },
    ),
  )
}
