/**
 * PnL series: an SVG path on a card, a Lightweight Charts canvas when one Alpha is open.
 * Chart colours come from the theme's CSS variables.
 */

import {
  BaselineSeries,
  type BaselineSeriesPartialOptions,
  ColorType,
  createChart,
  type IChartApi,
  LineSeries,
  LineStyle,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts'
import { useEffect, useMemo, useRef } from 'react'
import { cn } from '@/lib/cn'

/** A theme variable's value, since a canvas cannot read CSS variables (`@theme static` emits them all). */
export const theme = (name: string) =>
  getComputedStyle(document.documentElement).getPropertyValue(`--${name}`).trim()

/**
 * A colour token as `rgba()` at an opacity. Tokens are `oklch()`, which the chart library cannot
 * parse, so the browser paints one pixel and the pixel is read back as sRGB.
 */
export const color = (name: string, alpha = 1) => {
  const context = document.createElement('canvas').getContext('2d', { willReadFrequently: true })
  if (!context) return theme(name)
  context.fillStyle = theme(name)
  context.fillRect(0, 0, 1, 1)
  const [r, g, b] = context.getImageData(0, 0, 1, 1).data
  return `rgba(${r}, ${g}, ${b}, ${alpha})`
}

const COMPACT = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 2 })

/** An axis figure as 1.2M or 340K. */
export const compact = (price: number) => COMPACT.format(price)

/** The look every chart here shares; each adds its own options with `applyOptions`. */
export function baseChart(node: HTMLElement): IChartApi {
  const hairline = color('color-hairline')
  const guide = color('color-ink-tertiary')
  const tag = color('color-surface-4')
  return createChart(node, {
    autoSize: true,
    layout: {
      background: { type: ColorType.Solid, color: 'transparent' },
      textColor: color('color-ink-subtle'),
      fontFamily: theme('font-mono'),
      fontSize: 11,
      attributionLogo: false,
      panes: { separatorColor: hairline, enableResize: false },
    },
    grid: { vertLines: { visible: false }, horzLines: { color: hairline } },
    rightPriceScale: { borderVisible: false },
    // Ten years of days is ~2,500 bars; the default 0.5px minimum cannot fit them, and the
    // chart silently drops the early years instead.
    //
    // `fitContent` fits the width the chart first paints at. A later resize — the sidebar
    // opening, a split pane dragged — otherwise keeps that bar spacing, and the history
    // slides off the right edge; locking the range rescales it to the new width instead.
    timeScale: { borderVisible: false, minBarSpacing: 0.01, lockVisibleTimeRangeOnResize: true },
    crosshair: {
      vertLine: { color: guide, style: LineStyle.Dashed, labelBackgroundColor: tag },
      horzLine: { color: guide, style: LineStyle.Dashed, labelBackgroundColor: tag },
    },
  })
}

/** The dotted zero line a PnL or Sharpe series is read against. */
export const zeroLine = () => ({
  price: 0,
  color: color('color-ink-tertiary'),
  lineStyle: LineStyle.Dotted,
  lineWidth: 1 as const,
  axisLabelVisible: false,
})

/** Distance below the running peak, filled in the loss colour. */
export function addUnderwater(
  chart: IChartApi,
  pane?: number,
  options: BaselineSeriesPartialOptions = {},
) {
  const loss = 'color-pnl-negative'
  return chart.addSeries(
    BaselineSeries,
    {
      baseValue: { type: 'price', price: 0 },
      topLineColor: 'transparent',
      topFillColor1: 'transparent',
      topFillColor2: 'transparent',
      bottomLineColor: color(loss),
      bottomFillColor1: color(loss, 0.05),
      bottomFillColor2: color(loss, 0.35),
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
      ...options,
    },
    pane,
  )
}

/**
 * The card sparkline, drawn as one SVG path rather than a canvas: a screen can hold hundreds of
 * cards, and browsers silently drop all but a few dozen live 2D contexts.
 */
export function Sparkline({ values, label }: { values: number[]; label: string }) {
  const points = useMemo(() => {
    if (values.length < 2) return ''
    const low = Math.min(...values)
    const span = Math.max(...values) - low || 1
    const last = values.length - 1
    return values
      .map(
        (v, i) => `${((i / last) * 100).toFixed(2)},${(95 - ((v - low) / span) * 90).toFixed(2)}`,
      )
      .join(' ')
  }, [values])

  return (
    <div role="img" aria-label={label} className="h-12 w-full min-w-0">
      <svg viewBox="0 0 100 100" preserveAspectRatio="none" className="h-full w-full">
        <title>{label}</title>
        <polyline
          points={points}
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          vectorEffect="non-scaling-stroke"
          className="text-ink-muted"
        />
      </svg>
    </div>
  )
}

/**
 * Cumulative PnL in neutral ink: the path matters, not whether it ended up or down. With `dates`
 * (one `YYYY-MM-DD` per value) the axis shows real trading days; without them it counts points.
 */
export function PnlChart({
  values,
  dates,
  label = 'Cumulative PnL',
  className,
}: {
  values: number[]
  dates?: string[]
  label?: string
  className?: string
}) {
  const element = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const node = element.current
    if (!node || values.length < 2) return
    const times = dates && dates.length === values.length ? dates : null
    const time = (index: number): Time => times?.[index] ?? ((index + 1) as UTCTimestamp)
    const chart = baseChart(node)
    chart.applyOptions({
      // Real dates use the library's own date labels; a bare count needs spelling out.
      ...(times ? {} : { timeScale: { tickMarkFormatter: (t: Time) => String(t) } }),
      localization: {
        ...(times
          ? {}
          : { timeFormatter: (t: Time) => `Day ${Number(t).toLocaleString('en-US')}` }),
        priceFormatter: compact,
      },
      handleScroll: false,
      handleScale: false,
    })
    const series = chart.addSeries(LineSeries, {
      color: color('color-ink-muted'),
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: true,
    })
    series.setData(values.map((value, index) => ({ time: time(index), value })))
    series.createPriceLine(zeroLine())
    let peak = Number.NEGATIVE_INFINITY
    addUnderwater(chart, 1).setData(
      values.map((value, index) => {
        peak = Math.max(peak, value)
        return { time: time(index), value: value - peak }
      }),
    )
    chart.panes()[1]?.setStretchFactor(0.35)
    chart.timeScale().fitContent()
    return () => chart.remove()
  }, [values, dates])

  return (
    <div
      ref={element}
      role="img"
      aria-label={label}
      className={cn('h-72 w-full min-w-0', className)}
    />
  )
}
