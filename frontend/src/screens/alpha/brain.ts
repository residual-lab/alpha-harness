/**
 * The Alpha page read the way BRAIN's own page reads it: its number and date formats, its
 * metrics, its periods, and its sentence for each check. Pure functions; the rules are BRAIN's.
 */

import {
  PLATFORM_COMPETITIONS_URL,
  PLATFORM_TIMEZONE,
  POWER_POOL_CHECK,
  POWER_POOL_DESCRIPTION_HEADINGS,
  POWER_POOL_DESCRIPTION_MIN_LENGTH,
} from '@/api/platform'
import type { AlphaCheck } from '@/api/types'
import { isNum, moment } from '@/lib/format'
import type { AlphaInfo } from './api'
import { CHECK_MESSAGES } from './messages'

// ── Figures ────────────────────────────────────────────────────────────────────────────────

const TWO = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const WHOLE = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 })
/** What a message prints for a bare number: up to three decimals. */
const PLAIN = new Intl.NumberFormat('en-US')

/** A figure as BRAIN prints it; in a check message a whole number drops its decimals. */
export function brainValue(value: number, type: string, inCheck = false): string {
  const scaled = type === 'percent' ? value * 100 : type === 'permyriad' ? value * 10_000 : value
  const text = (inCheck && scaled % 1 === 0 ? WHOLE : TWO).format(scaled)
  return type === 'percent' ? `${text}%` : type === 'permyriad' ? `${text} bps` : text
}

/** A table cell: blank where BRAIN has no figure, counts and labels as sent. */
export const cell = (value: unknown, type: string) =>
  !isNum(value)
    ? value == null
      ? ''
      : String(value)
    : type === 'integer'
      ? String(value)
      : brainValue(value, type)

export interface Metric {
  id: string
  title: string
  type: 'decimal' | 'percent' | 'permyriad'
  /** BRAIN's explanation, behind the dot beside the title. */
  tip: string
}

/** The six figures every summary row shows, in BRAIN's order. */
export const METRICS: Metric[] = [
  {
    id: 'sharpe',
    title: 'Sharpe',
    type: 'decimal',
    tip: 'Average measure of risk-adjusted returns; Sharpe = Avg. Annualized Returns /  Annualized Std. Dev. of Returns',
  },
  {
    id: 'turnover',
    title: 'Turnover',
    type: 'percent',
    tip: 'Average measure of daily trading activity; Turnover = Value Traded / Value Held',
  },
  {
    id: 'fitness',
    title: 'Fitness',
    type: 'decimal',
    tip: 'Hybrid metric that indicates overall performance. Higher values indicate better performance. Fitness = Sharpe * Sqrt( Abs( Returns ) / Max( Turnover, 0.125 ) )',
  },
  {
    id: 'returns',
    title: 'Returns',
    type: 'percent',
    tip: 'Annualized average gain or loss as a fraction of the invested amount. In WorldQuant BRAIN the invested amount is equal to half the book size',
  },
  {
    id: 'drawdown',
    title: 'Drawdown',
    type: 'percent',
    tip: 'Drawdown is the largest reduction in PnL during a given period, expressed as a percentage. Good Returns/Drawdown ratio >=1',
  },
  {
    id: 'margin',
    title: 'Margin',
    type: 'permyriad',
    tip: 'Average gain or loss per dollar traded; calculated as PnL divided by total dollars traded in a given time period. Suggested: > 4 basis points',
  },
]

export interface Column {
  id: string
  title: string
  type: string
}

/** The yearly table's columns; `stage` only colours the row. */
export const YEARLY: Column[] = [
  { id: 'year', title: 'Year', type: 'year' },
  { id: 'sharpe', title: 'Sharpe', type: 'decimal' },
  { id: 'turnover', title: 'Turnover', type: 'percent' },
  { id: 'fitness', title: 'Fitness', type: 'decimal' },
  { id: 'returns', title: 'Returns', type: 'percent' },
  { id: 'drawdown', title: 'Drawdown', type: 'percent' },
  { id: 'margin', title: 'Margin', type: 'permyriad' },
  { id: 'longCount', title: 'Long Count', type: 'integer' },
  { id: 'shortCount', title: 'Short Count', type: 'integer' },
]

/** The one-row table the OS period adds above the yearly one. */
export const OS_TABLE: Column[] = [
  { id: 'sharpe60', title: 'Sharpe 60', type: 'decimal' },
  { id: 'sharpe125', title: 'Sharpe 125', type: 'decimal' },
  { id: 'sharpe250', title: 'Sharpe 250', type: 'decimal' },
  { id: 'sharpe500', title: 'Sharpe 500', type: 'decimal' },
  { id: 'osISSharpeRatio', title: 'OS/IS Ratio', type: 'decimal' },
  { id: 'preCloseSharpe', title: 'Pre-Close Sharpe', type: 'decimal' },
  { id: 'preCloseSharpeRatio', title: 'Pre-Close Ratio', type: 'decimal' },
  { id: 'selfCorrelation', title: 'Self-Correlation', type: 'decimal' },
]

// ── Dates ──────────────────────────────────────────────────────────────────────────────────

const ZONED_DAY = new Intl.DateTimeFormat('en-US', {
  month: '2-digit',
  day: '2-digit',
  year: 'numeric',
  timeZone: PLATFORM_TIMEZONE,
  timeZoneName: 'short',
})
const STAMP = new Intl.DateTimeFormat('en-US', {
  weekday: 'short',
  month: '2-digit',
  day: '2-digit',
  year: 'numeric',
  hour: 'numeric',
  minute: '2-digit',
})
/** A date inside a check message. */
// BRAIN sends a bare day, which parses as UTC midnight: read in UTC, or west of it the message
// names the day before.
const MESSAGE_DAY = new Intl.DateTimeFormat('en-US', { timeZone: 'UTC' })

/** `09/30/2026 EDT`: a day on BRAIN's own clock, as its Created and Start Date read. */
export function platformDay(iso: string | null | undefined): string {
  // A bare day is that day in New York, not the UTC midnight that would read as the day before.
  const date = moment(iso?.length === 10 ? `${iso}T12:00:00Z` : iso)
  if (!date) return ''
  const part = (type: string) => ZONED_DAY.formatToParts(date).find((p) => p.type === type)?.value
  return `${part('month')}/${part('day')}/${part('year')} ${part('timeZoneName')}`
}

/** `Wed, 09/30/2026, 10:03 AM` on this computer's clock: Last saved, Last Run. */
export const stamp = (iso: string | null | undefined) => {
  const date = moment(iso)
  return date ? STAMP.format(date) : ''
}

// ── Periods ────────────────────────────────────────────────────────────────────────────────

export type Period = 'train' | 'test' | 'is' | 'os'

export interface Tab {
  period: Period
  label: string
  available: boolean
}

/**
 * The Period tabs. An unsubmitted Alpha with a test period offers TRAIN alone until the test
 * period is shown, then TRAIN, TEST and IS; any other Alpha, IS and OS.
 */
export function periodTabs(
  periods: Partial<Record<Period, unknown>>,
  testControl: boolean,
  testShown: boolean,
): Tab[] {
  const tab = (period: Period, available: boolean): Tab => ({
    period,
    label: period.toUpperCase(),
    available,
  })
  if (testControl)
    return [tab('train', true), tab('test', testShown), tab('is', testShown), tab('os', false)]
  return [tab('is', !!periods.is), tab('os', !!periods.os)]
}

/** The tab a section opens on: TEST once the test period shows, TRAIN while it hides, else the
 *  Alpha's own stage, production counting as OS. */
export const firstPeriod = (
  stage: string | null,
  testControl: boolean,
  testShown: boolean,
): Period =>
  testControl ? (testShown ? 'test' : 'train') : stage === 'OS' || stage === 'PROD' ? 'os' : 'is'

/** TRAIN and TEST are parts of IS: their checks and title are IS's. */
export const stageOf = (period: Period): 'is' | 'os' =>
  period === 'train' || period === 'test' ? 'is' : period

// ── Checks ─────────────────────────────────────────────────────────────────────────────────

/** Result groups in the order BRAIN lists them. */
export const RESULT_ORDER = ['PASS', 'FAIL', 'WARNING', 'ERROR', 'PENDING']

/** BRAIN's checks grouped by result, groups in BRAIN's order, each in the order sent. */
export function byResult(checks: AlphaCheck[]): [string, AlphaCheck[]][] {
  const groups = new Map<string, AlphaCheck[]>()
  for (const check of checks) {
    const result = check.result ?? 'PENDING'
    const group = groups.get(result)
    if (group) group.push(check)
    else groups.set(result, [check])
  }
  const rank = (result: string) => {
    const at = RESULT_ORDER.indexOf(result)
    return at < 0 ? RESULT_ORDER.length : at
  }
  return [...groups].sort(([a], [b]) => rank(a) - rank(b))
}

export interface Part {
  text: string
  bold?: boolean
  href?: string
}

/** Each check's figures as the metric they measure. */
const MEASURES: Record<string, 'decimal' | 'percent'> = {
  LOW_FITNESS: 'decimal',
  LOW_SHARPE: 'decimal',
  IS_LADDER_SHARPE: 'decimal',
  LOW_ROBUST_UNIVERSE_SHARPE: 'decimal',
  HIGH_TURNOVER: 'percent',
  HT_TURNOVER: 'percent',
  HT_INVESTABLE_MAX_TRADE_TURNOVER: 'percent',
  HT_INVESTABLE_MAX_POSITION_TURNOVER: 'percent',
  LOW_TURNOVER: 'percent',
  LOW_ROBUST_UNIVERSE_RETURNS: 'percent',
  LOW_RETURNS: 'percent',
  IMBALANCE: 'percent',
  CONCENTRATED_WEIGHT: 'percent',
}

const MATCHES = new Set([
  'MATCHES_THEMES',
  'MATCHES_COMPETITION',
  'MATCHES_PYRAMID',
  'MATCHES_CLASSIFICATION',
])

type Values = Record<string, unknown>

const shown = (value: unknown) =>
  value instanceof Date
    ? MESSAGE_DAY.format(value)
    : isNum(value)
      ? PLAIN.format(value)
      : value == null
        ? ''
        : String(value)

/** One of BRAIN's messages filled in, its `**bold**` runs kept apart. */
export function message(key: string, values: Values = {}): Part[] {
  const template =
    (values['value'] === null ? CHECK_MESSAGES[`${key}.null`] : undefined) ?? CHECK_MESSAGES[key]
  if (template === undefined) return []
  return template
    .split(/(\*\*.*?\*\*)/)
    .filter(Boolean)
    .map((run) => {
      const bold = run.length > 4 && run.startsWith('**') && run.endsWith('**')
      const text = (bold ? run.slice(2, -2) : run).replace(/\{(\w+)\}/g, (_, name: string) =>
        shown(values[name]),
      )
      return bold ? { text, bold } : { text }
    })
}

const records = (raw: unknown) =>
  Array.isArray(raw)
    ? raw.filter((x): x is Record<string, unknown> => typeof x === 'object' && x !== null)
    : []

const competition = (match: Record<string, unknown>): Part => ({
  text: String(match['name'] ?? ''),
  href: `${PLATFORM_COMPETITIONS_URL}${String(match['id'])}`,
})

/** BRAIN's sentence for one check, in the stage's own wording. */
export function checkMessage(check: AlphaCheck, stage: 'IS' | 'OS'): Part[] {
  const name = check.name
  const result = check.result ?? 'PENDING'
  const key = `${stage}.${name}`
  const c: Values = { ...check }
  if (isNum(c['ratio'])) c['ratio'] = brainValue(c['ratio'], 'percent', true)
  const measure = MEASURES[name]
  for (const k of ['value', 'limit', 'max', 'min']) {
    const v = c[k]
    if (v === undefined) continue
    c[k] = measure && isNum(v) ? brainValue(v, measure, true) : MATCHES.has(name) ? String(v) : v
  }
  for (const k of ['startDate', 'endDate', 'date']) {
    // An unreadable date stays as BRAIN wrote it: formatting an Invalid Date throws.
    if (typeof c[k] === 'string') c[k] = moment(c[k]) ?? c[k]
  }
  const said = (parts: Part[]) => (parts.length ? parts : [{ text: `${name} ${result}` }])

  if (name === 'LOW_ROBUST_UNIVERSE_SHARPE' && c['ratio'] != null)
    return said(message(`${key}.WITH_RATIO.${result}`, c))
  if (c['value'] === undefined) c['value'] = 'none'

  const listed =
    MATCHES.has(name) &&
    (result === 'PASS' ||
      result === 'WARNING' ||
      (name === 'MATCHES_THEMES' && result === 'PENDING' && c['themes']) ||
      (name === 'MATCHES_COMPETITION' && result === 'PENDING' && c['competitions']))
  if (listed) {
    const matches =
      name === 'MATCHES_THEMES' && c['themes']
        ? records(c['themes'])
        : name === 'MATCHES_COMPETITION' && c['competitions']
          ? records(c['competitions'])
          : name === 'MATCHES_PYRAMID' && c['pyramids']
            ? records(c['pyramids'])
            : name === 'MATCHES_CLASSIFICATION' && Array.isArray(c['value'])
              ? (c['value'] as unknown[]).map((v): Values => ({ name: v }))
              : records(result === 'PASS' ? c['matched'] : c['unmatched'])
    const [first] = matches
    const at = `${key}.${result}`
    if (first && matches.length === 1) {
      if (name === 'MATCHES_COMPETITION' && first['id'])
        return [
          ...message(`${at}.single.beginning`),
          competition(first),
          ...message(`${at}.single.end`),
        ]
      const multiplier = isNum(first['multiplier']) ? first['multiplier'].toString(10) : undefined
      return said(message(`${at}.single`, { ...c, ...first, multiplier }))
    }
    if (first && matches.length > 1) {
      if (name === 'MATCHES_COMPETITION' && first['id'])
        return [
          ...message(`${at}.multiple.beginning`),
          ...matches.flatMap((m, i) => [...(i ? [{ text: ', ' }] : []), competition(m)]),
          ...message(`${at}.multiple.end`),
        ]
      if (name === 'MATCHES_CLASSIFICATION')
        return said(message(`${at}.multiple`, { value: matches.map((m) => m['name']).join('; ') }))
      return said(
        message(
          `${at}.multiple`,
          isNum(first['multiplier'])
            ? {
                matches: matches.map((m) => `${m['name']} — ${m['multiplier']}`).join('; '),
                finalMultiplier: c['multiplier'],
                effective: c['effective'],
              }
            : { matches: matches.map((m) => m['name']).join(', ') },
        ),
      )
    }
  }

  if (name === 'DATA_DIVERSITY' && (result === 'WARNING' || result === 'FAIL'))
    return said(
      message(`${stage}.DATA_DIVERSITY.NOT_PASSED`, {
        dataCategoryName: c['dataCategoryName'],
        field: (Array.isArray(c['fields']) ? c['fields'] : []).map((f) => `"${f}"`).join(', '),
      }),
    )
  if (name === 'DATA_SET_AUTHORIZATION' && result === 'FAIL')
    return said(message(`${key}.${result}`, { datasets: [c['datasets']].flat().join(', ') }))
  if (name === 'OPERATOR_AUTHORIZATION' && result === 'FAIL')
    return said(message(`${key}.${result}`, { operators: [c['operators']].flat().join(', ') }))
  const weighed =
    name === 'CONCENTRATED_WEIGHT' && ((c['value'] && c['value'] !== 'none') || c['limit'])
  return said(message(`${key}${weighed ? '.WITH_VALUES' : ''}.${result}`, c))
}

// ── Power Pool ─────────────────────────────────────────────────────────────────────────────

/** While its Power Pool check is open, BRAIN holds the Alpha's description to its template. */
export const describedForPowerPool = (alpha: AlphaInfo) =>
  (alpha.periods.is?.checks ?? []).some(
    (c) => c.name === POWER_POOL_CHECK && (c.result === 'PASS' || c.result === 'PENDING'),
  )

/** Power Pool Correlation applies: the check above is open, or BRAIN classes it eligible. */
export const isPowerPool = (alpha: AlphaInfo) =>
  describedForPowerPool(alpha) ||
  alpha.classifications.some((c) => c.id === 'POWER_POOL:POWER_POOL_ELIGIBLE')

/** BRAIN's template, exactly as its Use the template button writes it. */
export const POWER_POOL_TEMPLATE = POWER_POOL_DESCRIPTION_HEADINGS.map((h) => `${h}: `).join('\n')

const TEMPLATE =
  /^\s*idea:(\s+(.+?)\n)\s*rationale for data used:\s+((.+?)\n)\s*rationale for operators used:(\s+(.+?))/is

/** The first of the template's headings missing, out of order or with nothing after it. */
function templateGap(text: string): string | undefined {
  const lower = text.toLowerCase()
  const starts: number[] = []
  for (const heading of POWER_POOL_DESCRIPTION_HEADINGS) {
    const at = lower.indexOf(`${heading.toLowerCase()}:`, starts.at(-1) ?? 0)
    if (at === -1) return heading
    starts.push(at)
  }
  return POWER_POOL_DESCRIPTION_HEADINGS.find(
    (heading, i) => !text.slice((starts[i] ?? 0) + heading.length + 1, starts[i + 1]).trim(),
  )
}

/** What BRAIN says is wrong with a Power Pool description, in its words, and which heading. */
export function descriptionProblems(text: string): string[] {
  const found = TEMPLATE.exec(text)
  const gap = templateGap(text)
  return [
    ...(text.trim().length < POWER_POOL_DESCRIPTION_MIN_LENGTH
      ? [`Ensure this field has at least ${POWER_POOL_DESCRIPTION_MIN_LENGTH} characters.`]
      : []),
    ...(found && [found[1], found[3], found[5]].every((part) => part?.trim())
      ? []
      : [
          'Ensure the description is following the provided template.' +
            (gap ? ` "${gap}:" is missing, out of order or empty.` : ''),
        ]),
  ]
}
