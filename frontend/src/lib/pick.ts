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
  from: From
  result: PickResult<From, Extra> | null
  start: (scope: Scope, ids: string[], from: From) => void
  finish: (extra?: Extra) => void
  /** A finished pick made without starting one: chosen here, then sent to `from`. */
  hand: (scope: Scope, ids: string[], from: From, extra?: Extra) => void
  cancel: () => void
  /**
   * Moves the pick to `scope`. What was picked belongs to a region and delay, so only a
   * universe change keeps it.
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
        from: first,
        result: null,
        start: (scope, ids, from) => set({ active: true, scope, ids, from, result: null }),
        finish: (extra) => {
          const { scope, ids, from } = get()
          set({
            active: false,
            scope: null,
            ids: [],
            result: scope ? { scope, ids, from, extra } : null,
          })
        },
        hand: (scope, ids, from, extra) =>
          set({ active: false, scope: null, ids: [], result: { scope, ids, from, extra } }),
        cancel: () => set({ active: false, scope: null, ids: [], result: null }),
        follow: (scope) => {
          const { scope: current, ids } = get()
          const moved = !current || current.region !== scope.region || current.delay !== scope.delay
          set({ scope, ids: moved ? [] : ids })
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
