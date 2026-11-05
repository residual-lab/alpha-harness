---
version: alpha
name: Alpha Harness Workstation
description: "The design system of Alpha Harness, a local-first quant workstation for WorldQuant BRAIN. A near-black canvas with a four-step surface ladder, hairline rules, and a shadow only under what floats; one chromatic accent, lavender, reserved for execution, focus and selection; every other hue earned by data — profit, loss, core status, operator category. Type is a six-step scale that stops at 20px for everything but the Dashboard's one sentence, because the screens are dense grids of figures rather than pages of prose. Figures are mono and tabular wherever they can be compared. Nothing decorative: if a colour appears, a number or a state put it there."

colors:
  primary: "#5e6ad2"
  on-primary: "#ffffff"
  primary-hover: "#828fff"
  primary-focus: "#5e69d1"
  canvas: "#010102"
  surface-1: "#0f1011"
  surface-2: "#141516"
  surface-3: "#18191a"
  surface-4: "#1f2022"
  hairline: "#23252a"
  hairline-strong: "#34343a"
  hairline-subtle: "#17181c"
  ink: "#f7f8f8"
  ink-muted: "#d0d6e0"
  ink-subtle: "#8a8f98"
  ink-tertiary: "#52565e"
  pnl-positive: "#27a644"
  pnl-negative: "#e5484d"
  pnl-positive-text: "#54cb67"
  pnl-negative-text: "#ff9792"
  status-running: "#3b82f6"
  status-queued: "#8a8f98"
  status-warning: "#f59e0b"
  status-idle: "#34343a"

typography:
  caption:
    fontFamily: Inter Variable
    fontSize: 11px
    fontWeight: 500
    lineHeight: 1.25
    letterSpacing: 0.1px
  body-compact:
    fontFamily: Inter Variable
    fontSize: 12px
    fontWeight: 400
    lineHeight: 1.35
  body:
    fontFamily: Inter Variable
    fontSize: 13px
    fontWeight: 400
    lineHeight: 1.4
    letterSpacing: -0.05px
  title:
    fontFamily: Inter Variable
    fontSize: 15px
    fontWeight: 500
    lineHeight: 1.3
    letterSpacing: -0.2px
  headline:
    fontFamily: Inter Variable
    fontSize: 20px
    fontWeight: 600
    lineHeight: 1.25
    letterSpacing: -0.4px
  display:
    fontFamily: Inter Variable
    fontSize: 28px
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: -0.6px
  eyebrow:
    fontFamily: Inter Variable
    fontSize: 11px
    fontWeight: 500
    lineHeight: 1.3
    letterSpacing: 0.55px
  mono-metric:
    fontFamily: JetBrains Mono Variable
    fontSize: 13px
    fontWeight: 500
    lineHeight: 1.2
    letterSpacing: -0.2px
    fontFeature: "'tnum', 'lnum'"
  num:
    fontFamily: JetBrains Mono Variable
    fontSize: 13px
    fontWeight: 400
    lineHeight: 1.4
    letterSpacing: -0.2px
    fontFeature: "'tnum', 'lnum'"

spacing:
  xs: 4px
  sm: 8px
  md: 12px
  lg: 16px
  gutter: 16px

rounded:
  xs: 3px
  sm: 5px
  md: 8px
  lg: 10px
  pill: 9999px

components:
  app-canvas:
    backgroundColor: "{colors.canvas}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
  panel:
    backgroundColor: "{colors.surface-1}"
    textColor: "{colors.ink}"
    rounded: "{rounded.lg}"
    padding: "{spacing.lg}"
  panel-header:
    backgroundColor: "{colors.surface-1}"
    textColor: "{colors.ink}"
    typography: "{typography.title}"
    padding: "{spacing.md}"
  rule:
    backgroundColor: "{colors.hairline}"
    height: 1px
  rule-strong:
    backgroundColor: "{colors.hairline-strong}"
    height: 1px
  rule-row:
    backgroundColor: "{colors.hairline-subtle}"
    height: 1px
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.on-primary}"
    typography: "{typography.body}"
    rounded: "{rounded.sm}"
    padding: 0 12px
    height: 32px
  button-primary-hover:
    backgroundColor: "{colors.primary-hover}"
  button-primary-active:
    backgroundColor: "{colors.primary-focus}"
  button-secondary:
    backgroundColor: "{colors.surface-2}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.sm}"
    padding: 0 12px
    height: 32px
  button-secondary-hover:
    backgroundColor: "{colors.surface-3}"
  button-secondary-active:
    backgroundColor: "{colors.surface-4}"
  button-ghost:
    backgroundColor: transparent
    textColor: "{colors.ink-muted}"
    typography: "{typography.body}"
    rounded: "{rounded.sm}"
  button-danger:
    backgroundColor: "{colors.surface-2}"
    textColor: "{colors.pnl-negative-text}"
    typography: "{typography.body}"
    rounded: "{rounded.sm}"
  input:
    backgroundColor: "{colors.surface-2}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.sm}"
    height: 32px
  input-border:
    backgroundColor: "{colors.ink-tertiary}"
    width: 1px
  input-border-hover:
    backgroundColor: "{colors.ink-subtle}"
  input-border-open:
    backgroundColor: "{colors.primary}"
  focus-ring:
    backgroundColor: "{colors.primary-hover}"
    size: 2px
  dashboard-hero-greeting:
    textColor: "{colors.ink}"
    typography: "{typography.display}"
  dashboard-hero:
    textColor: "{colors.ink-muted}"
    typography: "{typography.display}"
  dashboard-hero-figure:
    textColor: "{colors.primary}"
    typography: "{typography.display}"
  metric-label:
    textColor: "{colors.ink-subtle}"
    typography: "{typography.caption}"
  metric-value:
    textColor: "{colors.ink}"
    typography: "{typography.headline}"
  section-eyebrow:
    textColor: "{colors.ink-subtle}"
    typography: "{typography.eyebrow}"
  status-box:
    textColor: "{colors.ink-subtle}"
    typography: "{typography.body-compact}"
    rounded: "{rounded.xs}"
    height: 28px
  badge:
    backgroundColor: "{colors.surface-3}"
    textColor: "{colors.ink-muted}"
    typography: "{typography.mono-metric}"
    rounded: "{rounded.xs}"
    padding: 2px 6px
  badge-muted:
    backgroundColor: "{colors.surface-2}"
    textColor: "{colors.ink-subtle}"
  metric-badge-profit:
    textColor: "{colors.pnl-positive-text}"
    typography: "{typography.mono-metric}"
    rounded: "{rounded.xs}"
  metric-badge-loss:
    textColor: "{colors.pnl-negative-text}"
    typography: "{typography.mono-metric}"
    rounded: "{rounded.xs}"
  metric-badge-warn:
    textColor: "{colors.status-warning}"
    typography: "{typography.mono-metric}"
    rounded: "{rounded.xs}"
  data-table-header:
    textColor: "{colors.ink-subtle}"
    typography: "{typography.caption}"
    height: 32px
  data-table-row:
    backgroundColor: "{colors.surface-1}"
    textColor: "{colors.ink}"
    typography: "{typography.body-compact}"
    height: 28px
  data-table-row-hover:
    backgroundColor: "{colors.surface-2}"
  check-dot-pass:
    backgroundColor: "{colors.pnl-positive}"
    rounded: "{rounded.pill}"
    size: 6px
  check-dot-fail:
    backgroundColor: "{colors.pnl-negative}"
    rounded: "{rounded.pill}"
    size: 6px
  quota-gauge-track:
    backgroundColor: "{colors.surface-2}"
    rounded: "{rounded.pill}"
    height: 6px
  quota-gauge-fill:
    backgroundColor: "{colors.primary}"
    rounded: "{rounded.pill}"
    height: 6px
  core-slot-idle:
    backgroundColor: "{colors.surface-1}"
    rounded: "{rounded.xs}"
    size: 20px
  core-slot-idle-edge:
    backgroundColor: "{colors.status-idle}"
    width: 1px
  core-slot-queued:
    backgroundColor: "{colors.status-queued}"
    rounded: "{rounded.xs}"
    size: 20px
  core-slot-running:
    backgroundColor: "{colors.status-running}"
    rounded: "{rounded.xs}"
    size: 20px
  core-slot-warning:
    backgroundColor: "{colors.status-warning}"
    rounded: "{rounded.xs}"
    size: 20px
  ast-editor:
    backgroundColor: "{colors.canvas}"
    textColor: "{colors.ink}"
    typography: "{typography.num}"
    rounded: "{rounded.sm}"
    padding: "{spacing.md}"
---

## Overview

Alpha Harness is a workstation, not a website. A consultant opens it for ten minutes, reads a
grid of figures, dispatches simulations and leaves. Every decision below follows from that: the
canvas is near-black so the figures carry the contrast, the type scale stops at 20px because
nothing on screen is a headline, and colour is rationed so that a green number means profit
rather than emphasis.

The palette descends from Linear's product surfaces — the canvas, surface ladder, ink steps and
lavender accent are theirs, kept because they are a well-measured dark set. Everything past that
(the P&L and status semantics, the operator category hues, the pyramid ramp, the dense type
scale) is this application's own, and exists because a screen here needed it.

This file is the source of truth for tokens. `frontend/src/index.css` implements it and
`frontend/tools/oklch.ts` generates the ramps. When they disagree, fix this file first, then the
CSS.

## Colors

Colour runs in three tiers. A component may only reach for tier 2.

**Tier 1, primitives** (`--spec-*`, `--neutral-*`, `--lavender-*`, …) live in `:root`, outside
`@theme`, so no utility class can reach them. The `--spec-*` values are the hex codes in this
file's frontmatter, verbatim. The eleven-step ramps are generated in OKLCH by
`frontend/tools/oklch.ts`; edit there and paste the block it prints rather than hand-writing a
step.

**Tier 2, semantic tokens** (`--color-*` inside `@theme static`) are the only colours components
use. Tailwind's own palette is dropped (`--color-*: initial`), so an ad-hoc colour cannot slip
in: if `bg-blue-500` fails to compile, that is the system working. `static` emits every variable
because the canvas charts read them at runtime.

Tokens that sit on a surface are opaque mixes, not translucent overlays —
`color-mix(in oklab, <hue> 12%, var(--spec-surface-1))` rather than an alpha channel — so the
value in the token is the colour on screen and its contrast can be measured.

**Tier 3, component states** (`--btn-*`, `--field-*`, `--focus-ring`) name the rest, hover,
press, open, invalid and disabled colours of the kit primitives, and reference tier 2 only. They
exist so a control's states are listed in one place instead of scattered through class strings.
There are thirteen, and that is the ceiling; a new one earns its name by being read in more than
one component.

### What each colour means

- **Lavender** (`primary`, `primary-hover`, `primary-focus`, `on-primary`) — execution, focus and
  selection: the primary button, the focus ring, the selected row, the progress fill. Never
  decoration.
- **`pnl-positive` / `pnl-negative`** — a verdict on a figure, never chosen by hand. What the
  verdict is depends on what the figure is:
  - **Money** (Returns, PnL, a change to the Portfolio) by its sign, through `signTone(value)`:
    green made, red lost.
  - **Diagnostic ratios** (Sharpe, Fitness, Turnover, Margin, and their train, test and
    after-cost versions) against the limits the Alpha's own checks name (`LOW_SHARPE`,
    `LOW_FITNESS`, `LOW_TURNOVER`, `HIGH_TURNOVER`), through `cutoffTone` and `figureTone`: red
    when negative, an inverted signal; green once it meets its limit; plain `ink` between,
    because a positive Sharpe of 0.32 is noise, not profit. The limits are read, never written
    down here: they differ by market. Turnover has a band rather than a floor, so it is green
    inside it and red outside, where the Alpha fails outright. With no limit known — Margin,
    or a figure that is not one Alpha's — only red is earned.
  So a red figure always means lost money, an inverted signal or a failed check, and a green
  one always means money made or a cutoff met. These two paint marks, fills and edges; a
  figure is written in **`pnl-positive-text` / `pnl-negative-text`**, the same hues lifted in
  OKLCH lightness until they clear the Lc 60 floor (see Floors).
- **`status-idle` / `status-queued` / `status-running` / `status-warning`** — what a simulation
  core is doing. The four states of the core matrix in the top bar.
- **`ink` → `ink-muted` → `ink-subtle` → `ink-tertiary`** — four text steps. Body copy is `ink`,
  secondary is `ink-muted`, labels and hints are `ink-subtle`; `ink-tertiary` is for control
  borders, not for text.
- **`hairline` / `hairline-strong` / `hairline-subtle`** — the three rules. Ordinary edges,
  boundaries that must read as boundaries, and the line between table rows.

Three further families are derived in tier 2 rather than listed as primitives, because each is a
mix over a named surface:

- **`category-*`** — the six Template Lab operator categories (cross-sectional, time-series,
  group, arithmetic, logical, transformational), each a generated hue at 10% for the fill and 45%
  for the edge over `surface-2`. They identify a taxonomy and carry no judgement.
- **`pyramid-*`** — eleven steps of `pnl-positive` over `surface-1` (5 → 72%), one per Pyramid
  Multiplier BRAIN offers, mapped from the values actually present rather than an assumed range.
  Deepening rather than diverging: the lowest multiplier is the floor of the payout, not a loss,
  so nothing on this map is ever red. It tops out at 72% so white figures hold APCA 78.
- **`*-tint` / `*-edge`** — a semantic hue at 12% (fill) and 40% (outline) over its surface, for
  judged badges and notices.

### Floors

Contrast is judged in **APCA**, not WCAG 2, because WCAG 2's ratio is known to misjudge light
text on near-black and this canvas is #010102. Measured by `frontend/tools/contrast.ts`, over
`surface-1`, on every `lefthook run check`: each figure below must still be what the token
measures, and each token must clear the floor its verdict names.

| Text | APCA Lc | WCAG 2 | Verdict |
| --- | --- | --- | --- |
| `ink` | 102.7 | 17.90:1 | Body text at any size |
| `ink-muted` | 81.0 | 13.04:1 | Body text at any size |
| `pnl-positive-text` | 61.7 | 9.17:1 | Figures a consultant acts on |
| `pnl-negative-text` | 61.5 | 9.16:1 | Figures a consultant acts on |
| `status-warning` | 60.0 | 8.87:1 | Fluent text, 14px+ |
| `pnl-positive` | 43.0 | 6.01:1 | Marks, fills and edges; not text |
| `ink-subtle` | 41.5 | 5.86:1 | Labels, headers, hints and punctuation |
| `pnl-negative` | 35.8 | 4.87:1 | Marks, fills and edges; not text |
| `ink-tertiary` | 16.0 | 2.59:1 | Non-text only, which is all it is used for |

Text has three floors, by what it is for:

- **Lc 75 — sentences**, anything a consultant reads as prose: `ink`, `ink-muted`.
- **Lc 60 — figures and statuses they act on**: `pnl-positive-text`, `pnl-negative-text`,
  `status-warning`. The two `-text` tokens hold it on every surface a figure sits on, not just
  `surface-1`: the lowest is 60.2, on the profit and loss tints.
- **Lc 40 — structural chrome**: column headers, metric labels, hints and expression
  punctuation, in `ink-subtle`. After the first day these are landmarks, not reading: they
  orient the eye to the figure beneath and should recede from it. Lifting them would flatten
  that order — and narrow the gap between brackets and numbers in an expression — while still
  landing under Lc 60.

APCA is the stricter judge here — `ink-subtle` passes WCAG AA at 5.86:1 — which is why deferring
to it is not a relaxation. `frontend/tools/contrast.ts` checks the table, both `-text` tokens on
their tints, and white on the deepest Pyramid step on every `lefthook run check`.

One rule holds regardless of contrast: no colour is the only carrier of a state. A red tile also
has a label; a green number also has a sign.

## Typography

Two families: **Inter Variable** for text, **JetBrains Mono Variable** for figures and code, both
bundled through `@fontsource-variable` so nothing is fetched at runtime.

Six text sizes — 11 / 12 / 13 / 15 / 20 / 28px — and Tailwind's scale is dropped
(`--text-*: initial`) so there is no seventh. `headline` at 20px is the ordinary ceiling: a page
title. Anything that wants to be louder than that is a metric, not a bigger font.

`display` at 28px is the single exception, and it is spoken for: the Dashboard hero, which is the
greeting and the one sentence about the day. That sentence is the product's whole argument — a
consultant's allowance expires unused every night — and the pair is the first thing read in a
ten-minute visit, so it is the one place where type does the work instead of a figure. Within the
block the greeting takes `ink` and the sentence `ink-muted`, so two lines of the same size still
have an order to read them in. A use of `display` anywhere else is a bug in this file, not a new
convention.

Tracking goes negative as size grows (−0.05px at `body`, −0.6px at `display`) and slightly
positive on `eyebrow` (+0.55px, i.e. 0.05em), which marks it as taxonomy rather than prose.
`eyebrow` is always set uppercase; `caption` is uppercased when it labels a metric or a table
column.

Figures are mono and tabular. The `body` rule sets `font-variant-numeric: tabular-nums` globally
so numbers inside sentences align; a standalone figure takes `num`, and a metric that will be
compared down a column takes `mono-metric`. A column of Sharpe ratios that does not align is a
bug.

## Layout

Spacing is a 4px scale used at four steps and no more: `xs` inside a control (icon to label),
`sm` between controls in a row, `md` between cards in a grid, `lg` between panels and as panel
padding.

A screen is a page container (`flex flex-col gap-4 p-3 lg:p-4`) holding a page header and
panels. The canvas shows through the gaps, and that gutter is the only background anyone sees
between panels.

Panels stretch; they do not centre. There is no fixed-width container, because a 34-inch monitor
should show more rows, not the same rows with wider margins. The one width limit is on prose: a
page description stops at 48rem.

Two-pane screens use `SplitPane`, a draggable split from 1024px up and a stacked column below
it, with each layout persisted per id in `localStorage`.

## Elevation & Depth

Elevation in the page is a surface step, never a shadow. The one shadow in the system belongs to
what floats over the page — tooltips, menus, select and builder popovers, dialogs, sheets, the
command menu, Settings, the peeking sidebar and toasts — and it is always the same one,
`shadow-float`:

```text
0 8px 24px -6px  black 70%    the soft drop onto whatever it covers
0 2px 6px        black 45%    the contact shadow along its edge
```

Both mix `--black` over transparent in oklab. The canvas is near-black, so a shadow as faint as a
light theme's would vanish; these darken a surface-1 row under a tooltip from 31 to 14 at its edge,
fading back over about 12px. It says "this is above the page and can be dismissed", which a
surface step alone cannot: a tooltip at surface-4 over a hovered row at surface-2 reads as part of
the row. Tailwind's shadow scale is dropped (`--shadow-*: initial`), so no other can creep in.

A modal — a dialog, a sheet, ⌘K, Settings — also dims and blurs the page behind it (`BACKDROP` in
`overlay.tsx`: black at 60% and a small backdrop blur), so the one thing in focus is the thing
asking for it. Settings is a modal rather than a screen for the same reason ⌘K is: a choice made
there should not cost the place the consultant was at.

```text
canvas      the gutter behind everything
surface-1   panels, table rows, dialog bodies
surface-2   controls, boxed metrics, hovered rows
surface-3   pressed controls, neutral badges
surface-4   the deepest lift: an active pressed control
```

Rules are hairlines: `hairline` for ordinary edges, `hairline-strong` where an edge has to read
as a boundary (a status box, a table header), `hairline-subtle` for the line between table rows.
The base layer sets `border-color: var(--color-hairline)` on `*, ::before, ::after`, so a bare
`border` utility is already correct — this replaces Tailwind v4's own `currentColor` default,
which would otherwise paint borders in the text colour.

The `panel-highlight` utility adds `inset 0 1px 0 0 hairline-strong`: a one-pixel top edge that
lifts a panel off the canvas without a shadow.

## Shapes

Five radii, and a rectangle is the default. `xs` (3px) for badges, tiles and the small chrome
that sits inside a row; `sm` (5px) for buttons and inputs; `md` (8px) for boxed metrics and
grouped controls; `lg` (10px) for panels and dialogs; `pill` for the quota gauge, the status
LEDs and the switch only — nothing else is fully round.

Corners never mix within one surface: a panel at `lg` holds buttons at `sm` holding badges at
`xs`, each step inward tightening.

## Components

`frontend/src/ui/` holds the primitives, and nothing outside it may define one.

- **kit.tsx** — `Page`, `PageHeader`, `Panel`, `Button`, `Input`, `Textarea`, `Field`, `Fieldset`,
  `Checkbox`, `Switch`, `Chips`, `Segmented`, `Metric`, `Badge`, `MetricBadge`, `KV`, `Progress` (also the
  quota gauge), `Kbd`, `Spinner`, `Skeleton`, `Notice`, `ErrorNotice`, `Empty`, `Disclosure`,
  `TabBar`, and the tone helpers (`signTone`, `cutoffTone`, `figureTone`, `checkTone`,
  `TEXT_TONE`).
- **table.tsx** — `DataTable`, virtualised and server-sorted, and `Pager`.
- **overlay.tsx** — `Dialog`, `Sheet`, `Confirm`, `Select`, `Menu`, `Tooltip`, on Base UI.
- **panels.tsx** — `SplitPane`, `ResizeHandle`, `useMediaQuery`.
- **scope-picker.tsx** — `ScopePicker`: Region, Delay and Universe, from BRAIN's settings schema.

All of them are headless primitives (Base UI, cmdk, TanStack, react-resizable-panels) skinned
with the tokens above. No component library, no CSS-in-JS.

Two domain components have no generic equivalent. The **core slots** are one tile per concurrent
simulation core, always present in the top bar, cycling through the four `status-*` colours. An
idle core is a hollow socket — `surface-1` inside a dashed `status-idle` edge — so it reads as
room to fill rather than as a disabled control; a filled tile is busy. Colour is never the only
mark: a running core fills along its bottom edge as its simulation goes, and a warning carries
a `!`. The
**AST editor** renders an Alpha expression on the canvas colour in mono, with operators in
lavender, fields in `ink`, numbers in `ink-muted` and punctuation in `ink-subtle`.

`Badge` and `MetricBadge` are deliberately separate: a badge labels (a status, a region), a
metric badge judges (a number, tinted by its tone). Merging them behind one variant prop would
let a label be tinted as though it were a verdict.

## Do's and Don'ts

- Do reach for a tier-2 token, always.
- Do let the value pick the colour: `signTone(returns)`, `figureTone('sharpe', sharpe, limits)`,
  `checkTone(result)`.
- Do use `num` for every standalone figure and `mono-metric` for every comparable metric.
- Do add a state to tier 3 only when a second component needs the same one.
- Do keep panels flush to the viewport width.
- Don't add a hex code outside tier 1.
- Don't introduce a seventh type size, and don't reach for `display` outside the Dashboard hero.
- Don't use a shadow for elevation in the page; step the surface. `shadow-float` is for floating
  surfaces only.
- Don't colour something to draw attention. Attention is the `attention` animation, and it is for
  onboarding only.
- Don't tint a label as though it were a judged number.
- Don't centre a page in a fixed-width container.

## Motion

Motion is functional and short. Transitions run on `colors` and `transform`; nothing eases longer
than 300ms.

- `sweep` — an ink shimmer crossing a market tile while its fields download.
- `attention` — a slow amber flash on a nav item the consultant has not opened yet.
- Progress and gauge fills animate `transform: scaleX()`, on the compositor, never `width`.

`prefers-reduced-motion: reduce` collapses every animation and transition to 0.01ms globally.

## Accessibility

- Every interactive element keeps the global `:focus-visible` ring. Do not remove it; if it
  clips, tighten `outline-offset` instead.
- Icon-only controls carry `aria-label` and `title` with the same words.
- Composite widgets use their real ARIA roles and keep the ownership chain intact: a
  `role="row"` must be owned by a `rowgroup` or the `table`, so any wrapper element in between
  takes `role="presentation"`.
- A button that navigates is not `aria-pressed`. If it looks selected, say so in its label.
- Grid-like screens implement roving tabindex rather than dropping keyboard access.
- Nothing states a value by colour alone.

## Iteration Guide

1. Changing a palette value: edit the frontmatter here, then `--spec-*` in `index.css`.
2. Adding a hue (a seventh operator category, say): generate the ramp with
   `frontend/tools/oklch.ts`, paste the eleven steps into tier 1, then add the 10% fill and 45%
   edge mixes to tier 2.
3. Adding a component state: put it in tier 3 only if a second component reads it.
4. `lefthook run check` validates this file's structure, the Floors table against the colours
   (`frontend/tools/contrast.ts`), and the code's types and formatting.

## Known Gaps

- **Borders are prose, not tokens.** The DESIGN.md component schema has no `borderColor`, so the
  hairline rules appear here as 1px `rule` components and the real border assignments live in
  `index.css`.
- **One theme.** `color-scheme: dark` is fixed and there is no light palette. The tier separation
  would support one, but no screen has been designed for it.
- **`DataTable` is the only dense grid primitive.** The simulation matrix and the sync matrix each
  hand-roll their own ARIA grid rather than sharing one.
