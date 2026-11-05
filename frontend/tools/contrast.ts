/**
 * The colour gate (DESIGN.md → Floors): every text token in the Floors table, measured in APCA
 * over `surface-1`, must still read what the table says and still clear its floor, and the
 * figures set on tinted ground must hold theirs there too.
 *   node frontend/tools/contrast.ts DESIGN.md
 *
 * The colours and the table are both read from DESIGN.md, so a palette change that moves a
 * figure fails here until the table is updated, and one that drops a token below its floor
 * fails until it is lifted. Node runs this file as it is; nothing is installed for it.
 */

import { readFileSync } from 'node:fs'

/** APCA 0.0.98G-4g: Lc of `text` over `background`, as a magnitude (dark or light text alike). */
function apca(text: string, background: string): number {
  const luminance = (hex: string) => {
    const [r, g, b] = [1, 3, 5].map((i) => Number.parseInt(hex.slice(i, i + 2), 16) / 255)
    const y = 0.2126729 * (r ?? 0) ** 2.4 + 0.7151522 * (g ?? 0) ** 2.4 + 0.072175 * (b ?? 0) ** 2.4
    // biome-ignore lint/suspicious/noApproximativeNumericConstant: APCA's own constant, not √2
    return y < 0.022 ? y + (0.022 - y) ** 1.414 : y
  }
  const [yt, yb] = [luminance(text), luminance(background)]
  if (Math.abs(yb - yt) < 0.0005) return 0
  const sapc = yb > yt ? (yb ** 0.56 - yt ** 0.57) * 1.14 : (yb ** 0.65 - yt ** 0.62) * 1.14
  const clipped = Math.abs(sapc) < 0.1 ? 0 : Math.abs(sapc) - 0.027
  return clipped * 100
}

/** The floor a row's verdict promises: prose, a figure acted on, chrome, or none for non-text. */
function floorOf(verdict: string): number {
  if (/body text/i.test(verdict)) return 75
  if (/figures a consultant acts on|fluent text/i.test(verdict)) return 60
  if (/labels|headers|hints|punctuation/i.test(verdict)) return 40
  return 0
}

const design = readFileSync(process.argv[2] ?? 'DESIGN.md', 'utf8')
const colors = new Map(
  [...design.matchAll(/^ {2}([a-z0-9-]+): "(#[0-9a-f]{6})"$/gim)].map((m) => [m[1], m[2]]),
)
const surface = colors.get('surface-1')
if (!surface) throw new Error('DESIGN.md has no surface-1 colour')

const failures: string[] = []
const rows = [...design.matchAll(/^\| `([a-z0-9-]+)` \| ([\d.]+) \| [^|]+ \| ([^|]+) \|$/gm)]
if (rows.length === 0) failures.push('The Floors table has no rows to check.')
for (const [, token = '', stated = '', verdict = ''] of rows) {
  const hex = colors.get(token)
  if (!hex) {
    failures.push(`${token}: in the Floors table but not among the colours.`)
    continue
  }
  const lc = apca(hex, surface)
  if (Math.abs(lc - Number(stated)) > 0.5)
    failures.push(`${token}: the table says Lc ${stated}, it measures ${lc.toFixed(1)}.`)
  const floor = floorOf(verdict)
  if (lc < floor) failures.push(`${token}: Lc ${lc.toFixed(1)} is under its Lc ${floor} floor.`)
}

// ── Figures on the surfaces mixed from their own hue ────────────────────────────────────

type Lab = [number, number, number]

const decode = (v: number) => (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4)
const linearOfHex = (hex: string): Lab =>
  [1, 3, 5].map((i) => decode(Number.parseInt(hex.slice(i, i + 2), 16) / 255)) as Lab

/** Linear sRGB to OKLab, Björn Ottosson's matrices. */
function oklab([r, g, b]: Lab): Lab {
  const [l, m, s] = [
    0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b,
    0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b,
    0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b,
  ].map(Math.cbrt) as Lab
  return [
    0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
    1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s,
  ]
}

function linearOfLab([L, a, b]: Lab): Lab {
  const [l, m, s] = [
    (L + 0.3963377774 * a + 0.2158037573 * b) ** 3,
    (L - 0.1055613458 * a - 0.0638541728 * b) ** 3,
    (L - 0.0894841775 * a - 1.291485548 * b) ** 3,
  ]
  return [
    4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s,
  ]
}
const encode = (v: number) => (v <= 0.0031308 ? 12.92 * v : 1.055 * v ** (1 / 2.4) - 0.055)
const hexOf = (rgb: Lab) =>
  `#${rgb
    .map((v) =>
      Math.round(encode(Math.min(1, Math.max(0, v))) * 255)
        .toString(16)
        .padStart(2, '0'),
    )
    .join('')}`

const sheet = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8')
/** `--color-<token>` as the sheet mixes it: one spec hue over one spec surface, in OKLab. */
function mixed(token: string): string {
  const m = new RegExp(
    `--color-${token}: color-mix\\(\\s*in oklab,\\s*var\\(--spec-([a-z0-9-]+)\\) (\\d+)%,\\s*var\\(--spec-([a-z0-9-]+)\\)`,
  ).exec(sheet)
  const [hue, ground] = [colors.get(m?.[1] ?? ''), colors.get(m?.[3] ?? '')]
  if (!m || !hue || !ground) throw new Error(`index.css: cannot read --color-${token}`)
  const share = Number(m[2]) / 100
  const [p, q] = [oklab(linearOfHex(hue)), oklab(linearOfHex(ground))]
  return hexOf(linearOfLab(p.map((v, i) => v * share + (q[i] ?? 0) * (1 - share)) as Lab))
}

/** DESIGN.md's promises for figures on tinted ground: the `-text` tokens hold their floor on
 *  their own tints, and white holds Lc 78 on the deepest Pyramid step. */
const GROUNDS: [text: string, ground: string, floor: number][] = [
  ['pnl-positive-text', 'pnl-positive-tint', 60],
  ['pnl-negative-text', 'pnl-negative-tint', 60],
  ['#ffffff', 'pyramid-10', 78],
]
for (const [text, ground, floor] of GROUNDS) {
  const hex = text.startsWith('#') ? text : colors.get(text)
  if (!hex) throw new Error(`DESIGN.md has no ${text} colour`)
  const lc = apca(hex, mixed(ground))
  if (lc < floor) failures.push(`${text} on ${ground}: Lc ${lc.toFixed(1)}, under ${floor}.`)
}

if (failures.length) {
  for (const failure of failures) console.error(failure)
  process.exit(1)
}
