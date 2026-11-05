/**
 * One of the Alpha's series as BRAIN draws it: the main line coloured by period (train, then
 * test, then out of sample), BRAIN's other lines beside it, cut at the test period while that
 * stays hidden.
 */

import { useQuery } from '@tanstack/react-query'
import { type ISeriesApi, LineSeries, LineStyle, type Time } from 'lightweight-charts'
import { useEffect, useRef, useState } from 'react'
import { cn } from '@/lib/cn'
import { isNum } from '@/lib/format'
import { baseChart, color, compact } from '@/screens/pool/pnl-chart'
import { ErrorNotice, Notice, Skeleton } from '@/ui/kit'
import { alpha as api, type Recordset, type RecordsetName } from './api'

/** BRAIN's three periods; the train colour also stands for the whole of IS. */
const PERIOD = {
  train: 'color-status-running',
  test: 'color-status-warning',
  os: 'color-pnl-positive',
}

interface Look {
  color: string
  dashed?: boolean
}

/** BRAIN's lines beside the main one, each in a colour of its own. */
const SIDE: Record<string, Look> = {
  'risk-neutralized-pnl': { color: 'color-ink-subtle' },
  'investability-constrained-pnl': { color: 'color-ink-tertiary', dashed: true },
}

/** A region-agnostic Alpha has a line per region and no main one. */
const REGION_LINES = [
  'color-status-running',
  'color-status-warning',
  'color-pnl-positive',
  'color-link',
]

/** Each line's look; the main line has none of its own, its periods colour it. */
function looks(lines: { name: string }[], main: string) {
  let region = 0
  return new Map(
    lines.map((p): [string, Look | null] => [
      p.name,
      SIDE[p.name] ??
        (p.name === main
          ? null
          : { color: REGION_LINES[region++ % REGION_LINES.length] ?? PERIOD.train }),
    ]),
  )
}

const AXIS: Record<string, (value: number) => string> = {
  amount: compact,
  decimal: (value) => value.toFixed(2),
  percent: (value) => `${value.toFixed(2)}%`,
}

export function AlphaChart({
  alphaId,
  name,
  testStart,
  osStart,
  testShown,
}: {
  alphaId: string
  name: RecordsetName
  testStart: string | null
  osStart: string | null
  /** False while the test period is hidden: the series stops where it starts. */
  testShown: boolean
}) {
  const set = useQuery({
    queryKey: ['alpha', alphaId, 'recordset', name],
    queryFn: () => api.recordset(alphaId, name),
    staleTime: Number.POSITIVE_INFINITY,
    retry: false,
  })
  const [hidden, setHidden] = useState<string[]>([])
  const element = useRef<HTMLDivElement>(null)
  // Each line by name, so showing or hiding one leaves the chart, and its zoom, as it is.
  const series = useRef(new Map<string, ISeriesApi<'Line'>>())
  const hiddenNow = useRef(hidden)
  hiddenNow.current = hidden
  const lines = (set.data?.schema?.properties ?? []).filter((p) => p.name !== 'date')
  /** Each line's value on the day under the pointer. */
  const [hover, setHover] = useState<{ date: string; values: Map<string, number> } | null>(null)

  useEffect(() => {
    const node = element.current
    const properties = set.data?.schema?.properties ?? []
    const at = properties.findIndex((p) => p.name === 'date')
    if (!node || at < 0) return
    const cut = testShown ? null : testStart
    const rows = (set.data?.records ?? []).filter((r) => cut === null || String(r[at]) < cut)
    const main = properties.find((p) => p.name === name)?.type ?? 'amount'
    const chart = baseChart(node)
    chart.applyOptions({ localization: { priceFormatter: AXIS[main] ?? compact } })
    const zone = { train: color(PERIOD.train), test: color(PERIOD.test), os: color(PERIOD.os) }
    const zoneOf = (date: string) =>
      osStart !== null && date >= osStart
        ? zone.os
        : testStart !== null && date >= testStart
          ? zone.test
          : zone.train
    const look = looks(properties, name)
    properties.forEach((p, i) => {
      if (p.name === 'date') return
      const side = look.get(p.name)
      const line = chart.addSeries(LineSeries, {
        color: side ? color(side.color) : zone.train,
        lineWidth: 1,
        lineStyle: side?.dashed ? LineStyle.Dashed : LineStyle.Solid,
        priceLineVisible: false,
        lastValueVisible: false,
        visible: !hiddenNow.current.includes(p.name),
      })
      series.current.set(p.name, line)
      line.setData(
        rows.map((r) => {
          const date = String(r[at])
          const value = r[i]
          if (!isNum(value)) return { time: date as Time }
          return {
            time: date as Time,
            value: p.type === 'percent' ? value * 100 : value,
            ...(side ? {} : { color: zoneOf(date) }),
          }
        }),
      )
    })
    chart.timeScale().fitContent()
    chart.subscribeCrosshairMove((point) => {
      if (point.time === undefined) {
        setHover(null)
        return
      }
      const values = new Map<string, number>()
      for (const [key, line] of series.current) {
        const at = point.seriesData.get(line)
        if (at && 'value' in at) values.set(key, at.value)
      }
      setHover({ date: String(point.time), values })
    })
    const lines = series.current
    return () => {
      lines.clear()
      chart.remove()
    }
  }, [set.data, name, testStart, osStart, testShown])

  useEffect(() => {
    for (const [key, line] of series.current) line.applyOptions({ visible: !hidden.includes(key) })
  }, [hidden])

  if (set.isPending) return <Skeleton className="h-80" label="Loading the chart from BRAIN" />
  if (set.isError) return <ErrorNotice error={set.error} title="The chart did not load" />
  if (!set.data.records?.length)
    return (
      <p className="py-10 text-center text-body text-ink-subtle">
        Chart not available, please clone this alpha and re-simulate.
      </p>
    )
  const title = set.data.schema?.title ?? name
  const look = looks(lines, name)
  const reading = (p: (typeof lines)[number]) => {
    const value = hover?.values.get(p.name)
    return value === undefined ? null : (AXIS[p.type ?? 'amount'] ?? compact)(value)
  }
  // The main line changes colour at each period it crosses: say which colour is which.
  const periods = lines.some((p) => p.name === name)
    ? [
        { label: testStart || osStart ? 'Train' : 'IS', color: PERIOD.train },
        ...(testStart && testShown ? [{ label: 'Test', color: PERIOD.test }] : []),
        ...(osStart ? [{ label: 'OS', color: PERIOD.os }] : []),
      ]
    : []
  const main = lines.length === 1 ? lines[0] : undefined
  return (
    <div className="flex flex-col gap-3">
      {set.data.problem && <Notice tone="warn">{set.data.problem}</Notice>}
      <div ref={element} role="img" aria-label={`${title} of ${alphaId}`} className="h-80 w-full" />
      <div className="flex flex-wrap items-center justify-center gap-x-5 gap-y-1 text-body-compact">
        {periods.length > 1 &&
          periods.map((period) => (
            <span key={period.label} className="flex items-center gap-1.5 text-ink-subtle">
              <span
                aria-hidden
                className="w-4 border-t-2"
                style={{ borderColor: `var(--${period.color})` }}
              />
              {period.label}
            </span>
          ))}
        <span className="num min-w-24 text-ink-subtle">
          {hover?.date ?? ''}
          {main && reading(main) && <span className="ml-2 text-ink">{reading(main)}</span>}
        </span>
      </div>
      {lines.length > 1 && (
        <ul className="flex flex-wrap items-center justify-center gap-x-5 gap-y-1">
          {lines.map((p) => {
            const off = hidden.includes(p.name)
            const side = look.get(p.name)
            return (
              <li key={p.name}>
                <button
                  type="button"
                  aria-pressed={!off}
                  onClick={() =>
                    setHidden(off ? hidden.filter((h) => h !== p.name) : [...hidden, p.name])
                  }
                  className={cn(
                    'flex items-center gap-1.5 text-body-compact transition-colors hover:text-ink',
                    off ? 'text-ink-disabled' : 'text-ink-muted',
                  )}
                >
                  <span
                    aria-hidden
                    className={cn('w-4 border-t', side?.dashed && 'border-dashed')}
                    style={{ borderColor: `var(--${side?.color ?? PERIOD.train})` }}
                  />
                  {p.title ?? p.name}
                  {!off && reading(p) && <span className="num text-ink">{reading(p)}</span>}
                </button>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

/** BRAIN's before-and-after colours: the pool as it is, then with this Alpha in it. */
const PAIR = ['color-status-warning', 'color-status-running']

/** Every series of a recordset drawn as its own line, with a legend: the comparison's PnL. */
export function PairChart({ set, label }: { set: Recordset; label: string }) {
  const element = useRef<HTMLDivElement>(null)
  const properties = set.schema?.properties ?? []
  const lines = properties.filter((p) => p.name !== 'date')

  useEffect(() => {
    const node = element.current
    const all = set.schema?.properties ?? []
    const at = all.findIndex((p) => p.name === 'date')
    if (!node || at < 0) return
    const chart = baseChart(node)
    chart.applyOptions({ localization: { priceFormatter: compact } })
    all
      .filter((p) => p.name !== 'date')
      .forEach((p, i) => {
        const column = all.indexOf(p)
        chart
          .addSeries(LineSeries, {
            color: color(PAIR[i % PAIR.length] ?? 'color-ink'),
            lineWidth: 1,
            priceLineVisible: false,
            lastValueVisible: false,
          })
          .setData(
            (set.records ?? []).map((r) => {
              const value = r[column]
              const time = String(r[at]) as Time
              return isNum(value) ? { time, value } : { time }
            }),
          )
      })
    chart.timeScale().fitContent()
    return () => chart.remove()
  }, [set])

  return (
    <div className="flex flex-col gap-2">
      <div ref={element} role="img" aria-label={label} className="h-64 w-full" />
      <ul className="flex flex-wrap items-center justify-center gap-x-5 gap-y-1 text-body-compact text-ink-muted">
        {lines.map((p, i) => (
          <li key={p.name} className="flex items-center gap-1.5">
            <span
              aria-hidden
              className="w-4 border-t"
              style={{ borderColor: `var(--${PAIR[i % PAIR.length]})` }}
            />
            {p.title ?? p.name}
          </li>
        ))}
      </ul>
    </div>
  )
}
