/** Cumulative PnL with the train and test periods in two colours, and drawdown beneath it. */

import { LineSeries, TickMarkType, type Time } from 'lightweight-charts'
import { useEffect, useRef } from 'react'
import { BOOK_SIZE } from '@/api/platform'
import { addUnderwater, baseChart, color, compact, zeroLine } from '@/screens/pool/pnl-chart'

/** Drawdown is stated as BRAIN states it, over half this book. */
const BOOK = BOOK_SIZE

export const TRAIN_COLOR = 'color-status-running'
export const TEST_COLOR = 'color-status-warning'

/** Distance below the running peak each day, as a fraction of half the book. */
function underwater(points: { date: string; value: number }[], bookSize: number) {
  let peak = Number.NEGATIVE_INFINITY
  return points.map((p) => {
    peak = Math.max(peak, p.value)
    return { date: p.date, value: (p.value - peak) / (bookSize / 2) }
  })
}

const yearOf = (time: Time) =>
  typeof time === 'object' ? String(time.year) : String(time).slice(0, 4)

export function PortfolioChart({
  dates,
  curve,
  testStart,
  label,
}: {
  dates: string[]
  curve: number[]
  testStart: string | null
  label: string
}) {
  const element = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const node = element.current
    if (!node || curve.length < 2) return
    const chart = baseChart(node)
    chart.applyOptions({
      // Years only: a lone month label at the edge reads as a gap in the data.
      timeScale: {
        tickMarkFormatter: (time: Time, type: TickMarkType) =>
          type === TickMarkType.Year ? yearOf(time) : '',
      },
      // The whole history always fits; dragging or zooming only pushed it off the edge.
      handleScroll: false,
      handleScale: false,
    })

    const points = dates.map((date, i) => ({ time: date as Time, value: curve[i] ?? 0 }))
    const line = (name: string) =>
      chart.addSeries(LineSeries, {
        color: color(name),
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: false,
        priceFormat: { type: 'custom', formatter: compact },
      })
    // The split day belongs to both periods, so the two lines meet rather than leave a gap.
    const train = testStart ? points.filter((p) => String(p.time) <= testStart) : points
    const test = testStart ? points.filter((p) => String(p.time) >= testStart) : []
    const trainSeries = line(TRAIN_COLOR)
    trainSeries.setData(train)
    trainSeries.createPriceLine(zeroLine())
    if (test.length) line(TEST_COLOR).setData(test)

    addUnderwater(chart, 1, {
      priceFormat: { type: 'custom', formatter: (p: number) => `${(p * 100).toFixed(2)}%` },
    }).setData(
      underwater(
        dates.map((date, i) => ({ date, value: curve[i] ?? 0 })),
        BOOK,
      ).map((p) => ({ time: p.date as Time, value: p.value })),
    )
    chart.panes()[1]?.setStretchFactor(0.35)
    chart.timeScale().fitContent()
    return () => chart.remove()
  }, [dates, curve, testStart])

  return <div ref={element} role="img" aria-label={label} className="h-[26rem] w-full min-w-0" />
}

export function ChartLegend({ testStart }: { testStart: string | null }) {
  const swatch = (name: string, text: string) => (
    <span className="flex items-center gap-1.5">
      <span className="h-0.5 w-4" style={{ background: `var(--${name})` }} aria-hidden />
      {text}
    </span>
  )
  return (
    <p className="flex flex-wrap items-center gap-4 text-body-compact text-ink-subtle">
      {swatch(TRAIN_COLOR, testStart ? 'Train Period' : 'PnL')}
      {testStart && swatch(TEST_COLOR, 'Test Period')}
      {swatch('color-pnl-negative', 'Drawdown')}
    </p>
  )
}
