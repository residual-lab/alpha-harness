import fc from 'fast-check'
import { expect, test } from 'vitest'
import type { SimulationRow } from '@/api/types'
import { buildCores, type Core, coreBlocks, coreCost } from './matrix'

const row = (fields: Partial<SimulationRow> & Pick<SimulationRow, 'id'>): SimulationRow => ({
  alphaId: null,
  childIds: [],
  createdAt: null,
  delay: 1,
  elapsedSeconds: null,
  expression: null,
  finishedAt: null,
  instrumentType: 'EQUITY',
  isBatch: false,
  language: 'FASTEXPR',
  message: null,
  parentId: null,
  platformId: null,
  platformStatus: null,
  progress: null,
  region: 'USA',
  settings: null,
  simType: 'REGULAR',
  status: 'RUNNING',
  submittedAt: null,
  task: 'search',
  universe: null,
  ...fields,
})

/** An active snapshot: batches, their children (or orphans of a parent gone), standalones. */
const snapshots = fc
  .array(
    fc.record({
      kind: fc.constantFrom('standalone', 'batch', 'child'),
      parent: fc.nat({ max: 29 }),
      status: fc.constantFrom('QUEUED', 'PENDING', 'RUNNING', 'COMPLETE', 'ERROR'),
      region: fc.constantFrom('USA', 'GLB', 'EUR'),
      simType: fc.constantFrom('REGULAR', 'REGION_AGNOSTIC', 'RA_PARENT', 'SUPER'),
    }),
    { maxLength: 30 },
  )
  .map((specs) =>
    specs.map(({ kind, parent, status, region, simType }, i) =>
      row({
        id: i + 1,
        isBatch: kind === 'batch',
        parentId: kind === 'child' ? parent + 1 : null,
        status,
        region,
        simType,
      }),
    ),
  )
const slotCounts = fc.integer({ min: 1, max: 12 })
const batchSizes = fc.integer({ min: 1, max: 10 })

const isLive = (r: SimulationRow) => r.status === 'PENDING' || r.status === 'RUNNING'
const holdersOf = (active: SimulationRow[]) =>
  active.filter((r) => isLive(r) && (r.isBatch || r.parentId == null))

function expectLaidOut(cores: Core[], slots: number) {
  const held = Array.from({ length: slots }, () => 0)
  for (const core of cores) {
    expect(core.span).toBe(coreCost(core.holder))
    expect(core.start).toBeGreaterThanOrEqual(0)
    expect(core.start + core.span).toBeLessThanOrEqual(slots)
    for (let i = core.start; i < core.start + core.span; i++) held[i] = (held[i] ?? 0) + 1
  }
  expect(held.every((n) => n <= 1)).toBe(true)
}

test('every running simulation gets cores of its own, within the machine', () => {
  fc.assert(
    fc.property(snapshots, slotCounts, batchSizes, (active, slots, maxBatch) => {
      const { cores, overflow } = buildCores(active, slots, maxBatch)
      expectLaidOut(cores, slots)
      const holders = holdersOf(active)
      expect(cores.length + overflow).toBe(holders.length)
      // Slots are a count, not places: nobody is left undrawn while they would all fit.
      if (holders.reduce((n, h) => n + coreCost(h), 0) <= slots) expect(overflow).toBe(0)

      const drawn = cores.flatMap((core) => core.cells.flatMap((cell) => cell.row ?? []))
      expect(new Set(drawn.map((r) => r.id)).size).toBe(drawn.length)
      for (const core of cores) {
        expect(core.cells).toHaveLength(maxBatch)
        for (const { row: alpha, state } of core.cells) {
          if (!alpha) continue
          expect(isLive(alpha)).toBe(true)
          expect(state).toBe(alpha.status === 'RUNNING' ? 'RUNNING' : 'PENDING')
          expect(core.holder.isBatch ? alpha.parentId : alpha.id).toBe(core.holder.id)
        }
      }
    }),
  )
})

test('a simulation keeps its cores while others come and go', () => {
  fc.assert(
    fc.property(
      snapshots,
      fc.array(fc.boolean(), { maxLength: 30 }),
      slotCounts,
      batchSizes,
      (active, finished, slots, maxBatch) => {
        const first = buildCores(active, slots, maxBatch)
        expect(buildCores(active, slots, maxBatch, first.assignment).assignment).toEqual(
          first.assignment,
        )

        const rest = active.filter((_, i) => !finished[i])
        const next = buildCores(rest, slots, maxBatch, first.assignment)
        expectLaidOut(next.cores, slots)
        const kept = [...next.assignment].every(([id, start]) => {
          const before = first.assignment.get(id)
          return before === undefined || before === start
        })
        // Moving everyone is allowed only to draw a simulation that would otherwise be left
        // out, and then the layout is the one a fresh start gives.
        if (!kept) {
          expect(next.overflow).toBe(0)
          expect(next.assignment).toEqual(buildCores(rest, slots, maxBatch).assignment)
        }
      },
    ),
  )
})

test('the blocks tile the machine: every core once, every idle core alone', () => {
  fc.assert(
    fc.property(snapshots, slotCounts, batchSizes, (active, slots, maxBatch) => {
      const { cores } = buildCores(active, slots, maxBatch)
      const blocks = coreBlocks(cores, slots)
      let next = 0
      for (const block of blocks) {
        expect(block.start).toBe(next)
        next += block.span
        if (!block.core) expect(block.span).toBe(1)
      }
      expect(next).toBe(slots)
      expect(blocks.flatMap((b) => b.core ?? [])).toEqual(
        [...cores].sort((a, b) => a.start - b.start),
      )
    }),
  )
})
