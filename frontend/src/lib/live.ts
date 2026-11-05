/**
 * Hot state pushed over the socket, held outside React. Components read a slice with a
 * selector (`useLive((s) => s.simulations)`), so a tick re-renders only what reads it.
 */

import { useEffect, useMemo } from 'react'
import { create } from 'zustand'
import type { SimulationRow, SyncRun, TasksSummary } from '@/api/types'
import { buildCores } from './matrix'
import { telemetry } from './ws'

/**
 * One stable core assignment shared by every matrix view, so the Simulation Matrix page and
 * the header preview put each batch on the same core, and a cancel never shifts the rest.
 */
let coreAssignment: ReadonlyMap<number, number> = new Map()

type Built = ReturnType<typeof buildCores>
/** The last layout and what it was built from: the header and the matrix ask with the same
 *  snapshot on every tick, and the second is answered without building it again. */
let lastBuilt: {
  active: SimulationRow[] | null | undefined
  slots: number
  maxBatch: number
  from: ReadonlyMap<number, number>
  built: Built
} | null = null

function coresFor(
  active: SimulationRow[] | null | undefined,
  slots: number,
  maxBatch: number,
): Built {
  const last = lastBuilt
  if (
    last &&
    last.active === active &&
    last.slots === slots &&
    last.maxBatch === maxBatch &&
    last.from === coreAssignment
  )
    return last.built
  const built = buildCores(active ?? [], slots, maxBatch, coreAssignment)
  lastBuilt = { active, slots, maxBatch, from: coreAssignment, built }
  return built
}

export function useCores(
  active: SimulationRow[] | null | undefined,
  slots: number,
  maxBatch: number,
) {
  const built = useMemo(() => coresFor(active, slots, maxBatch), [active, slots, maxBatch])
  // Kept after the render commits, never during it: a render may be thrown away (StrictMode
  // runs each one twice), and a discarded pass must not move everyone's core.
  useEffect(() => {
    coreAssignment = built.assignment
  }, [built])
  return built
}

interface Live {
  connected: boolean
  /** The full active set, replaced on every `simulations` message. Null until the first. */
  simulations: SimulationRow[] | null
  tasks: TasksSummary | null
  sync: SyncRun | null
  /** Set when any call reports BRAIN wants identity verification; cleared on dismiss. */
  verificationUrl: string | null
}

export const useLive = create<Live>(() => ({
  connected: false,
  simulations: null,
  tasks: null,
  sync: null,
  verificationUrl: null,
}))

// A snapshot from before a disconnect may be long stale, and a restarted backend has no tasks
// message to replay for jobs that died with it; dropping both re-arms the REST fallbacks.
telemetry.onStatus((connected) =>
  useLive.setState(connected ? { connected } : { connected, simulations: null, tasks: null }),
)
telemetry.subscribe('simulations', (payload) => {
  // The topic also carries `{alphaId, stored}` from backfill; only arrays are snapshots.
  if (Array.isArray(payload)) useLive.setState({ simulations: payload as SimulationRow[] })
})
telemetry.subscribe('tasks', (payload) => useLive.setState({ tasks: payload as TasksSummary }))
telemetry.subscribe('sync', (payload) => useLive.setState({ sync: payload as SyncRun }))
