/** The Settings screen's choices, read wherever they apply, and the labs' own recipes. */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import type { StoreApi } from 'zustand'
import { type Preferences, preferences } from '@/api/core'
import { errorMessage, http } from '@/api/http'

const KEY = ['preferences']

/** What a new task gets until Settings has loaded; the same as Settings' own default. */
const FALLBACK_CORES = 4

export function usePreferences() {
  // The usual staleness, not forever: another open tab can change them, and a save sends the
  // whole set, so a copy held indefinitely would put that tab's change back.
  return useQuery({ queryKey: KEY, queryFn: preferences.get })
}

/** Saves a whole set of choices, showing it at once and putting the old one back if it fails. */
export function useSavePreferences() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: preferences.put,
    onMutate: async (next: Preferences) => {
      await queryClient.cancelQueries({ queryKey: KEY })
      const before = queryClient.getQueryData<Preferences>(KEY)
      queryClient.setQueryData(KEY, next)
      return { before }
    },
    onError: (error, _next, context) => {
      if (context?.before) queryClient.setQueryData(KEY, context.before)
      toast.error('That setting was not saved', { description: errorMessage(error) })
    },
    onSuccess: (saved) => queryClient.setQueryData(KEY, saved),
  })
}

/** A task form's cores: what it chose, or Settings' default until it chooses. */
export function useCores(chosen: number | null): number {
  const saved = usePreferences()
  return chosen ?? saved.data?.defaultCores ?? FALLBACK_CORES
}

/**
 * Keeps a lab's own recipes in harness.db as well as in this browser, so a reinstall or another
 * browser still finds them. harness.db's copy wins once read and every change after is saved
 * there; until it holds one, the copy this browser kept is uploaded instead. Returns the hook
 * that reads it, for the screen to say when it cannot. `what` names them in a failed save.
 */
export function keepInHarness<T extends object>(store: StoreApi<T>, path: string, what: string) {
  let read = false
  let saving = false
  let again = false
  // One save at a time, always of the latest state, so an older one can never land last.
  const save = async (): Promise<void> => {
    if (saving) {
      again = true
      return
    }
    saving = true
    try {
      await http.put(path, store.getState())
    } catch (error) {
      toast.error(`Your own ${what} were not saved`, { id: path, description: errorMessage(error) })
    }
    saving = false
    if (again) {
      again = false
      await save()
    }
  }
  store.subscribe(() => {
    if (read) void save()
  })
  return function useKept() {
    return useQuery({
      queryKey: ['kept', path],
      queryFn: async () => {
        const kept = await http.get<Partial<T> | null>(path)
        if (kept) store.setState(kept)
        // Only a copy with something in it: an empty one must not stand in for another
        // browser's that has yet to be read.
        else if (Object.values(store.getState()).some((v) => Array.isArray(v) && v.length > 0))
          await http.put(path, store.getState())
        read = true
        return true
      },
      staleTime: Number.POSITIVE_INFINITY,
    })
  }
}
