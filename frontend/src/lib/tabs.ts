/** Persisted drafts kept in step across browser tabs. */

type Persisted = {
  persist: { getOptions: () => { name?: string }; rehydrate: () => Promise<void> | void }
}

/** By storage name, one each: a module reloaded in development replaces its store here rather
 *  than adding a listener beside the old one's. */
const followed = new Map<string, Persisted>()

window.addEventListener('storage', (event) => {
  if (event.key) void followed.get(event.key)?.persist.rehydrate()
})

/**
 * Takes up what another tab saved as soon as it lands. Without it, the next change here writes
 * this tab's older copy over theirs, and the draft written in the other tab is gone.
 */
export function followOtherTabs(store: Persisted): void {
  const name = store.persist.getOptions().name
  if (name) followed.set(name, store)
}
