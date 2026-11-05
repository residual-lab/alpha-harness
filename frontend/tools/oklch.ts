/**
 * Offline generator for the token system (DESIGN.md → Colour): the operator ramps, built in
 * OKLCH and kept inside the sRGB gamut.
 *
 * The palette in `src/index.css` was generated here and is pasted in, so nothing at runtime
 * imports this: it lives in `tools/` beside the codegen, and `tsconfig.node.json` type-checks
 * it. Reach for it when adding a hue to the operator ramps.
 */

export type Oklch = { l: number; c: number; h: number }
type Triple = [number, number, number]

const rad = Math.PI / 180

function oklabOf({ l, c, h }: Oklch): Triple {
  return [l, c * Math.cos(h * rad), c * Math.sin(h * rad)]
}

function linearOf(color: Oklch): Triple {
  const [L, a, b] = oklabOf(color)
  const l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
  const m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
  const s = (L - 0.0894841775 * a - 1.291485548 * b) ** 3
  return [
    4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s,
  ]
}

const encode = (v: number) => (v <= 0.0031308 ? 12.92 * v : 1.055 * v ** (1 / 2.4) - 0.055)

/** Gamma-encoded sRGB, unclamped, so an out-of-gamut colour shows up outside 0–1. */
export const srgb = (color: Oklch): Triple => linearOf(color).map(encode) as Triple

export const inGamut = (color: Oklch) => srgb(color).every((v) => v >= -1e-6 && v <= 1 + 1e-6)

/** The largest in-gamut chroma at this lightness and hue. */
export function maxChroma(l: number, h: number): number {
  let [lo, hi] = [0, 0.4]
  for (let i = 0; i < 32; i++) {
    const mid = (lo + hi) / 2
    if (inGamut({ l, c: mid, h })) lo = mid
    else hi = mid
  }
  return lo
}

// ── Palette ─────────────────────────────────────────────────────────────────────────────

export const STOPS = [
  '050',
  '100',
  '200',
  '300',
  '400',
  '500',
  '600',
  '700',
  '800',
  '900',
  '950',
] as const
export const LIGHTNESS = [0.98, 0.94, 0.88, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.12] as const

/** Hue, the chroma a stop at L 0.55 would ask for before the gamut clamp, and the one stop
 *  of its scale the sheet uses. */
export const HUES: Record<string, [hue: number, peak: number, stop: (typeof STOPS)[number]]> = {
  neutral: [262, 0.012, '600'],
  lavender: [276, 0.16, '300'],
  orchid: [322, 0.13, '400'],
  teal: [227, 0.12, '400'],
  jade: [158, 0.12, '400'],
  copper: [58, 0.13, '400'],
  olive: [97, 0.12, '400'],
  rose: [354, 0.13, '400'],
}

const MID = 0.55
const SPAN = 0.43 // MID to either end of the scale
const DRIFT = 8 // degrees at the ends
const COOL = 264 // shadows lean blue
const WARM = 80 // highlights lean amber

/** Rotate `h` toward `target` by at most `max` degrees, the short way round. */
function toward(h: number, target: number, max: number): number {
  const d = ((target - h + 540) % 360) - 180
  return (h + Math.sign(d) * Math.min(Math.abs(d), max) + 360) % 360
}

/**
 * One tone of a hue: chroma follows a bell curve around L 0.55 and stays at 95% of the sRGB
 * boundary, so no stop clips; the hue drifts cooler in shadows and warmer in highlights.
 */
export function tone(l: number, [hue, peak]: [number, number]): Oklch {
  const t = (l - MID) / SPAN
  const h = Number(toward(hue, t < 0 ? COOL : WARM, DRIFT * Math.min(1, Math.abs(t))).toFixed(1))
  const wanted = peak * Math.exp(-(((l - MID) / 0.35) ** 2))
  const c = Math.floor(Math.min(wanted, 0.95 * maxChroma(l, h)) * 1000) / 1000
  return { l, c, h }
}

/** Shortest numerals, as Biome writes them, so the formatted sheet still equals the generator. */
export const css = ({ l, c, h }: Oklch) =>
  `oklch(${Number(l.toFixed(2))} ${Number(c.toFixed(3))} ${Number(h.toFixed(1))})`

/** DESIGN.md colours, verbatim: the spec is the source of truth, so these are not generated. */
export const SPEC: Record<string, string> = {
  primary: '#5e6ad2',
  'on-primary': '#ffffff',
  'primary-hover': '#828fff',
  'primary-focus': '#5e69d1',
  canvas: '#010102',
  'surface-1': '#0f1011',
  'surface-2': '#141516',
  'surface-3': '#18191a',
  'surface-4': '#1f2022',
  hairline: '#23252a',
  'hairline-strong': '#34343a',
  'hairline-subtle': '#17181c',
  ink: '#f7f8f8',
  'ink-muted': '#d0d6e0',
  'ink-subtle': '#8a8f98',
  'ink-tertiary': '#52565e',
  'pnl-positive': '#27a644',
  'pnl-negative': '#e5484d',
  'status-running': '#3b82f6',
  'status-queued': '#8a8f98',
  'status-warning': '#f59e0b',
  'status-idle': '#34343a',
}

/** Every Tier 1 primitive, name → value, in declaration order. */
export function primitives(): [string, string][] {
  const out: [string, string][] = [
    ['white', 'oklch(1 0 0)'],
    ['black', 'oklch(0 0 0)'],
    ...Object.entries(SPEC).map(([name, value]): [string, string] => [`spec-${name}`, value]),
  ]
  for (const [name, [hue, peak, stop]] of Object.entries(HUES)) {
    out.push([`${name}-${stop}`, css(tone(LIGHTNESS[STOPS.indexOf(stop)] ?? 0, [hue, peak]))])
  }
  return out
}
