/**
 * Alphas as key-value Markdown for an LLM to review. Whatever every Alpha shares is said once
 * at the top; what differs becomes headed sections (region, delay, universe, neutralization)
 * with one block of `- Key: Value` lines per Alpha, highest Sharpe first. Nothing is dropped:
 * a value is only moved to where it is said once.
 */

import { CORE_METRICS, CORE_ORDER } from '@/lib/format'

export interface CopyAlpha {
  expression: string | null
  sharpe: number | null
  /** Simulation settings, in the order they read. Shared ones go under "Simulation Settings". */
  settings: [string, string][]
  /** Other descriptions (classifications, pyramids…). Shared ones go under "Every Alpha". */
  traits: [string, string][]
  /** Figures, always per Alpha. */
  metrics: [string, string][]
}

/** Settings the sections branch on, outermost first, when they differ between Alphas. */
const BRANCHES = ['Region', 'Delay', 'Universe', 'Neutralization']

function sharedValues(rows: CopyAlpha[], pick: (r: CopyAlpha) => [string, string][]) {
  const labels = [...new Set(rows.flatMap((r) => pick(r).map(([label]) => label)))]
  const value = (r: CopyAlpha, label: string) => pick(r).find(([l]) => l === label)?.[1] ?? '—'
  const shared = labels.filter((l) => new Set(rows.map((r) => value(r, l))).size === 1)
  return { labels, shared, value }
}

export function kvMarkdown({
  title,
  alphas,
  extra = [],
  unit = 'Alpha',
}: {
  /** The first line, e.g. "198 Results". */
  title: string
  alphas: CopyAlpha[]
  /** Blocks placed after the shared ones, e.g. named check sets. */
  extra?: string[][]
  /** What a block is headed when nothing but its figures sets it apart: "Result 1", "Alpha 1". */
  unit?: string
}): string {
  const rows = [...alphas].sort(
    (a, b) => (b.sharpe ?? Number.NEGATIVE_INFINITY) - (a.sharpe ?? Number.NEGATIVE_INFINITY),
  )
  const settings = sharedValues(rows, (r) => r.settings)
  const traits = sharedValues(rows, (r) => r.traits)
  const branches = BRANCHES.filter(
    (b) => settings.labels.includes(b) && !settings.shared.includes(b),
  )
  const inline = settings.labels.filter(
    (l) => !settings.shared.includes(l) && !branches.includes(l),
  )
  const oneExpression = rows.length > 0 && new Set(rows.map((r) => r.expression ?? '')).size === 1

  const lines = [title, '']
  if (oneExpression) lines.push('Alpha Expression:', '```', rows[0]?.expression ?? '', '```', '')
  const block = (heading: string, labels: string[], value: (label: string) => string) => {
    if (!labels.length) return
    lines.push(heading, ...labels.map((l) => `${l}: ${value(l)}`), '')
  }
  const first = rows[0]
  if (first) {
    block('Simulation Settings', settings.shared, (l) => settings.value(first, l))
    block(`Every ${unit}`, traits.shared, (l) => traits.value(first, l))
  }
  for (const section of extra) lines.push(...section, '')

  let count = 0
  const ownTraits = traits.labels.filter((l) => !traits.shared.includes(l))
  const alpha = (r: CopyAlpha, level: number) => {
    count += 1
    // Headed by what sets it apart from its section; numbered when nothing does.
    const heading = inline.map((l) => `${l}: ${settings.value(r, l)}`).join(' · ')
    lines.push(`${'#'.repeat(level)} ${heading || `${unit} ${count}`}`)
    if (!oneExpression) lines.push(`- Expression: \`${(r.expression ?? '').replace(/\s+/g, ' ')}\``)
    for (const label of ownTraits) lines.push(`- ${label}: ${traits.value(r, label)}`)
    for (const [label, value] of r.metrics) lines.push(`- ${label}: ${value}`)
    lines.push('')
  }
  const section = (group: CopyAlpha[], depth: number) => {
    const branch = branches[depth]
    if (branch === undefined) {
      for (const r of group) alpha(r, depth + 2)
      return
    }
    // In the order their best Alpha appears, so the best section leads.
    const groups = new Map<string, CopyAlpha[]>()
    for (const r of group) {
      const value = settings.value(r, branch)
      const members = groups.get(value)
      if (members) members.push(r)
      else groups.set(value, [r])
    }
    for (const [value, members] of groups) {
      lines.push(`${'#'.repeat(depth + 2)} ${branch}: ${value}`, '')
      section(members, depth + 1)
    }
  }
  section(rows, 0)
  return lines.join('\n').trimEnd()
}

/** The metrics every copy carries, in the order the user reads them. */
export function coreMetrics(r: {
  sharpe: number | null
  turnover: number | null
  fitness: number | null
  returns: number | null
  drawdown: number | null
  margin: number | null
  longCount?: number | null | undefined
  shortCount?: number | null | undefined
}): [string, string][] {
  const count = (v: number | null | undefined) => (v == null ? '—' : String(v))
  return [
    ...CORE_ORDER.map((k): [string, string] => [CORE_METRICS[k].label, CORE_METRICS[k].show(r[k])]),
    ['Long Count', count(r.longCount)],
    ['Short Count', count(r.shortCount)],
  ]
}
