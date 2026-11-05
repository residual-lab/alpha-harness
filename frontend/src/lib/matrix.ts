/**
 * The 8×10 simulation matrix as data: one core per BRAIN slot, one cell per Alpha inside
 * its multi-simulation. QUEUED work is counted elsewhere, never drawn.
 */

import { GLB_REGION, GLB_SLOTS, RA_SLOTS } from '@/api/platform'
import type { SimulationRow } from '@/api/types'

export type CellState = 'RUNNING' | 'PENDING' | 'EMPTY'

export interface Cell {
  row: SimulationRow | null
  state: CellState
}

/** A running simulation and the run of cores it holds. */
export interface Core {
  holder: SimulationRow
  cells: Cell[]
  /** Zero-based index of its first core. */
  start: number
  /** How many cores it holds: see {@link coreCost}. */
  span: number
}

const cellState = (status: SimulationRow['status']): CellState =>
  status === 'RUNNING' ? 'RUNNING' : 'PENDING'

export const EMPTY: Cell = { row: null, state: 'EMPTY' }

/**
 * Give each holder a stable run of cores, as wide as it costs: BRAIN exposes no slot numbers,
 * so a holder keeps the cores it was first drawn on and a newcomer takes the lowest run free —
 * without this, a batch finishing on C1 moves every other one up a row. Pass `holders` in
 * arrival order (record id); one that fits nowhere is left out, to be counted as overflow.
 */
export function assignCores(
  holders: { id: number; cost: number }[],
  previous: ReadonlyMap<number, number>,
  slots: number,
): Map<number, number> {
  const next = new Map<number, number>()
  const taken = Array.from({ length: slots }, () => false)
  const fits = (start: number, cost: number) =>
    start + cost <= slots && taken.slice(start, start + cost).every((t) => !t)
  const take = (id: number, start: number, cost: number) => {
    next.set(id, start)
    for (let i = start; i < start + cost; i++) taken[i] = true
  }
  for (const { id, cost } of holders) {
    const start = previous.get(id)
    if (start !== undefined && fits(start, cost)) take(id, start, cost)
  }
  for (const { id, cost } of holders) {
    if (next.has(id)) continue
    const start = taken.findIndex((_, i) => fits(i, cost))
    if (start !== -1) take(id, start, cost)
  }
  // BRAIN's slots are a count, not places: when the kept cores leave a wide run no free cores
  // side by side, everyone moves up rather than a running simulation going undrawn.
  const fitsAll = holders.reduce((n, h) => n + h.cost, 0) <= slots
  if (next.size < holders.length && previous.size > 0 && fitsAll)
    return assignCores(holders, new Map(), slots)
  return next
}

/**
 * Cores one running simulation holds. BRAIN meters GLB at double rate, and a region-agnostic
 * run costs one core per region its fields intersect — so drawing a chip each would show a
 * bar that is mostly idle while the engine has no room left. As ``SLOT_COST`` in
 * ``engine/lifecycle.py`` counts it.
 */
export function coreCost(row: { region?: string | null; simType?: string | null }): number {
  if (row.simType === 'REGION_AGNOSTIC' || row.simType === 'RA_PARENT') return RA_SLOTS
  return row.region === GLB_REGION ? GLB_SLOTS : 1
}

/** A run and the cores it occupies, or one idle core. */
export interface CoreBlock {
  holder: SimulationRow | null
  core: Core | null
  /** How many cores this block covers. */
  span: number
  /** Zero-based index of its first core. */
  start: number
}

/**
 * The cores laid out as what actually holds them: one block per running simulation, as wide
 * as {@link coreCost} says and on the cores it was given, and an idle block for each free core.
 *
 * A GLB run holds two cores and a region-agnostic one holds three, so a row or a chip each
 * would draw a machine that looks half free while the engine has no room to dispatch —
 * and filling every core would claim work that is not there. A block spanning its cores says
 * both at once: how many simulations are running, and how much of the machine they occupy.
 */
export function coreBlocks(cores: Core[], slots: number): CoreBlock[] {
  const byStart = new Map(cores.map((core) => [core.start, core]))
  const blocks: CoreBlock[] = []
  for (let i = 0; i < slots; ) {
    const core = byStart.get(i)
    blocks.push(
      core
        ? { holder: core.holder, core, span: core.span, start: i }
        : { holder: null, core: null, span: 1, start: i },
    )
    i += core?.span ?? 1
  }
  return blocks
}

/** ``C3`` for one core, ``C3\u2013C4`` for a run holding several. */
export const blockLabel = (block: CoreBlock): string =>
  block.span === 1 ? `C${block.start + 1}` : `C${block.start + 1}\u2013C${block.start + block.span}`

/**
 * Group active rows into cores, passing the previous `assignment` to keep every holder on its
 * core between snapshots. When BRAIN finishes a batch the parent leaves the active set before
 * its children are collected, so those orphans no longer hold a core and are not drawn.
 */
export function buildCores(
  active: SimulationRow[],
  slots: number,
  maxBatch: number,
  previous: ReadonlyMap<number, number> = new Map(),
): { cores: Core[]; overflow: number; assignment: Map<number, number> } {
  const live = active.filter((r) => r.status === 'PENDING' || r.status === 'RUNNING')
  const parents = live.filter((r) => r.isBatch)
  const liveParents = new Set(parents.map((p) => p.id))
  const children = new Map<number, SimulationRow[]>()
  const standalones: SimulationRow[] = []
  const adopt = (parentId: number, row: SimulationRow) => {
    const siblings = children.get(parentId)
    if (siblings) siblings.push(row)
    else children.set(parentId, [row])
  }

  for (const row of live) {
    if (row.isBatch) continue
    if (row.parentId == null) standalones.push(row)
    else if (liveParents.has(row.parentId)) adopt(row.parentId, row)
  }

  const holders = [...parents, ...standalones].sort((a, b) => a.id - b.id)
  const assignment = assignCores(
    holders.map((h) => ({ id: h.id, cost: coreCost(h) })),
    previous,
    slots,
  )

  const cores = holders.flatMap((holder): Core[] => {
    const start = assignment.get(holder.id)
    if (start === undefined) return []
    // A batch's children are claimed in the same write that creates it, so each Alpha it
    // carries has its own cell; the rest of the core is empty, not busy.
    const alphas = holder.isBatch
      ? (children.get(holder.id) ?? []).sort((a, b) => a.id - b.id)
      : [holder]
    return [
      {
        holder,
        start,
        span: coreCost(holder),
        cells: Array.from({ length: maxBatch }, (_, j): Cell => {
          const row = alphas[j]
          return row ? { row, state: cellState(row.status) } : EMPTY
        }),
      },
    ]
  })
  return {
    cores,
    overflow: Math.max(0, holders.length - assignment.size),
    assignment,
  }
}
