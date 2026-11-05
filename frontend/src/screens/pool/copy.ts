/** Stored Alphas as key-value Markdown for an LLM to review (see `lib/llm-copy`). */

import { fmt } from '@/lib/format'
import { coreMetrics, kvMarkdown } from '@/lib/llm-copy'
import { type AlphaPageRequest, type AlphaRow, pool } from './api'

const PAGE = 500

/** Pages asked for at once after the first: measured on 96,000 Alphas, under half the time of
 *  one after another, and the same rows in the same order. */
const AT_ONCE = 4

/** Every Alpha the table's filters match, not only the page on screen. */
async function everyAlpha(body: AlphaPageRequest): Promise<AlphaRow[]> {
  const first = await pool.query({ ...body, limit: PAGE, offset: 0 })
  const count = Math.ceil(first.total / PAGE)
  const pages: AlphaRow[][] = [first.results]
  let next = 1
  const worker = async () => {
    while (next < count) {
      const at = next++
      pages[at] = (await pool.query({ ...body, limit: PAGE, offset: at * PAGE })).results
    }
  }
  await Promise.all(Array.from({ length: Math.min(AT_ONCE, count - 1) }, worker))
  return pages.flat()
}

const text = (v: string | number | null | undefined) => (v == null ? '—' : String(v))

export async function alphasMarkdown(body: AlphaPageRequest, noun: string): Promise<string> {
  const rows = await everyAlpha(body)
  return kvMarkdown({
    title: `${fmt.int(rows.length)} ${noun}`,
    alphas: rows.map((r) => ({
      expression: r.expression,
      sharpe: r.sharpe,
      settings: [
        ['Region', text(r.region)],
        ['Universe', text(r.universe)],
        ['Delay', text(r.delay)],
        ['Decay', text(r.decay)],
        ['Neutralization', text(r.neutralization)],
        ['Truncation', text(r.truncation)],
        ['Max Trade', text(r.maxTrade)],
        ['Max Position', text(r.maxPosition)],
      ],
      traits: [
        ['Type', text(r.type)],
        ['Status', text(r.status)],
        ['Classifications', r.classifications?.join(', ') || '—'],
        ['Pyramids', r.pyramids?.join(', ') || '—'],
        ['Operator Count', text(r.operatorCount)],
      ],
      metrics: [
        ...coreMetrics(r),
        ['Train Sharpe', fmt.ratio(r.trainSharpe)],
        ['Test Sharpe', fmt.ratio(r.testSharpe)],
        ['Submitted', fmt.date(r.dateSubmitted)],
      ],
    })),
  })
}
