/** Correlation of the selected Alphas' daily PnL, as a heatmap small enough to take in at once. */

import { memo, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { cn } from '@/lib/cn'
import { fmt } from '@/lib/format'
import type { PortfolioResult } from './api'

/** The lowest and highest correlation shown, which the colour scale runs between. */
type Range = { low: number; high: number }

function rangeOf(values: number[]): Range {
  return { low: Math.min(...values), high: Math.max(...values) }
}

const GREEN = 'var(--color-pnl-positive)'
const AMBER = 'var(--color-status-warning)'
const RED = 'var(--color-pnl-negative)'
/** Where amber sits on the scale. Squared, so the many pairs near the low end stay green and
 *  only the high ones heat up: amber lands at the square root of the halfway point. */
const AMBER_AT = Math.SQRT1_2

/** Green at the lowest correlation, red at the highest, through amber. */
function fill(value: number, { low, high }: Range) {
  const t = high > low ? ((value - low) / (high - low)) ** 2 : 1
  return t < 0.5
    ? `color-mix(in oklch, ${AMBER} ${Math.round(t * 200)}%, ${GREEN})`
    : `color-mix(in oklch, ${RED} ${Math.round((t - 0.5) * 200)}%, ${AMBER})`
}

function Legend({ range }: { range: Range }) {
  return (
    <div className="flex items-center justify-center gap-2 text-body-compact text-ink-muted">
      <span className="num">{fmt.ratio(range.low, 4)}</span>
      <span
        className="h-2 w-48 rounded-pill"
        style={{
          background: `linear-gradient(to right in oklch, ${GREEN}, ${AMBER} ${Math.round(AMBER_AT * 100)}%, ${RED})`,
        }}
        aria-hidden
      />
      <span className="num">{fmt.ratio(range.high, 4)}</span>
    </div>
  )
}

/** Opens an Alpha in the detail sheet, with the other one of its pair a step away. */
type Open = (alphaId: string, ids?: string[]) => void

/** Past its grid limit the backend sends only the most correlated pairs, highest first. */
function TopPairs({ result, onOpen }: { result: PortfolioResult; onOpen: Open }) {
  const { ids, topPairs, measuredPairs } = result
  const range = rangeOf(topPairs.map((p) => p.correlation))
  if (!topPairs.length) {
    return (
      <p className="text-body-compact text-ink-subtle">
        No two of these {ids.length} Alphas share the 250 trading days a correlation needs.
      </p>
    )
  }
  return (
    <div className="flex flex-col gap-3">
      <p className="text-body-compact text-ink-subtle">
        The {topPairs.length} most correlated of{' '}
        <span className="num">{fmt.int(measuredPairs)}</span> pairs; {ids.length} Alphas are too
        many for the grid. Click a pair to open it.
      </p>
      <div className="grid gap-1.5 sm:grid-cols-2 xl:grid-cols-4">
        {topPairs.map((p) => (
          <button
            type="button"
            key={`${p.a}-${p.b}`}
            onClick={() => onOpen(p.a, [p.a, p.b])}
            className="flex items-center justify-between gap-3 rounded-xs border border-hairline px-3 py-2 text-left text-body-compact transition-colors hover:border-hairline-strong hover:bg-surface-2"
          >
            <span className="flex items-center gap-2">
              <span
                className="size-3 rounded-xs"
                style={{ background: fill(p.correlation, range) }}
                aria-hidden
              />
              <span className="mono-metric text-ink-muted">
                {p.a} · {p.b}
              </span>
            </span>
            <span className="num text-ink">{fmt.ratio(p.correlation, 4)}</span>
          </button>
        ))}
      </div>
      <Legend range={range} />
    </div>
  )
}

/** A square and the gap after it, in CSS pixels. */
const CELL = 12
const PITCH = CELL + 1

/** Each square's colour as a canvas takes it, resolved by the browser once: the tokens are
 *  fixed for the session. */
const PAINT = new Map<string, string>()

/** Up to 22,500 squares, so one canvas rather than an element each. */
function paint(canvas: HTMLCanvasElement, { ids, correlation }: PortfolioResult, range: Range) {
  const context = canvas.getContext('2d')
  if (!context) return
  const resolved = (css: string) => {
    let colour = PAINT.get(css)
    if (colour === undefined) {
      canvas.style.color = css
      colour = getComputedStyle(canvas).color
      PAINT.set(css, colour)
    }
    return colour
  }
  context.clearRect(0, 0, canvas.width, canvas.height)
  ids.forEach((_, i) => {
    ids.forEach((_, j) => {
      const value = correlation[i]?.[j] ?? null
      context.fillStyle = resolved(
        i === j
          ? 'var(--color-surface-3)'
          : value === null
            ? 'var(--color-surface-2)'
            : fill(value, range),
      )
      context.fillRect(j * PITCH, i * PITCH, CELL, CELL)
    })
  })
  canvas.style.color = ''
}

function Grid({ result, onOpen }: { result: PortfolioResult; onOpen: Open }) {
  const { ids, correlation } = result
  const [at, setAt] = useState<[number, number] | null>(null)
  const canvas = useRef<HTMLCanvasElement>(null)
  const measured = useMemo(
    () => correlation.flatMap((row, i) => row.filter((v, j): v is number => j !== i && v !== null)),
    [correlation],
  )
  const range = useMemo(() => (measured.length ? rangeOf(measured) : null), [measured])
  useLayoutEffect(() => {
    if (canvas.current && range) paint(canvas.current, result, range)
  }, [result, range])
  // The backend decides: a grid it can send, or only the top pairs when it cannot.
  if (!correlation.length || !range) return <TopPairs result={result} onOpen={onOpen} />
  // None over the gaps between squares.
  const cell = ({ nativeEvent: { offsetX: x, offsetY: y } }: React.MouseEvent) =>
    x % PITCH < CELL && y % PITCH < CELL
      ? ([Math.floor(y / PITCH), Math.floor(x / PITCH)] as [number, number])
      : null
  const side = ids.length * PITCH - 1
  const [a, b] = at ? [ids[at[0]], ids[at[1]]] : [undefined, undefined]
  const value = at ? (correlation[at[0]]?.[at[1]] ?? null) : null
  return (
    <div className="flex flex-col gap-3">
      <p className="num flex h-5 items-center justify-center gap-2 text-body-compact">
        {a && b ? (
          a === b ? (
            <span className="text-ink">{a}</span>
          ) : (
            <>
              <span className="text-ink">{a}</span>
              <span className="text-ink-subtle">·</span>
              <span className="text-ink">{b}</span>
              <span className="text-ink-subtle">—</span>
              <span className="font-medium text-ink">
                {value === null ? 'under 250 shared days' : fmt.ratio(value, 4)}
              </span>
            </>
          )
        ) : (
          <span className="text-ink-subtle">
            Point at a square to read its pair; click to open it.
          </span>
        )}
      </p>
      <div className="overflow-x-auto">
        <div className="relative mx-auto w-fit">
          {/* A picture to point at; the members table is the keyboard's way to each Alpha. */}
          <canvas
            ref={canvas}
            width={side}
            height={side}
            role="img"
            aria-label={`Correlation of ${ids.length} Alphas, newest first`}
            // A canvas pixel per CSS pixel, squares on whole pixels: pixelated scaling keeps it
            // crisp at any devicePixelRatio without the backing store growing with it.
            className={cn('block bg-surface-1 [image-rendering:pixelated]', at && 'cursor-pointer')}
            onPointerMove={(e) => {
              const hit = cell(e)
              setAt((now) => (now?.[0] === hit?.[0] && now?.[1] === hit?.[1] ? now : hit))
            }}
            onPointerLeave={() => setAt(null)}
            onClick={(e) => {
              const hit = cell(e)
              const [row, column] = hit ? [ids[hit[0]], ids[hit[1]]] : []
              if (row && column) onOpen(row, row === column ? [row] : [row, column])
            }}
          />
          {at && (
            <div
              className="pointer-events-none absolute size-3 outline outline-1 outline-ink"
              style={{ top: at[0] * PITCH, left: at[1] * PITCH }}
            />
          )}
        </div>
      </div>
      <Legend range={range} />
    </div>
  )
}

/** Memoised: drawn again only when the result itself changes. */
export const CorrelationMatrix = memo(Grid)
