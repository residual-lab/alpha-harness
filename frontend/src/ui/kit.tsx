/**
 * The small primitives every screen composes. Linear rules: surfaces carry hierarchy
 * (canvas → surface-1 panel → surface-2 row), hairlines divide, lavender marks interaction
 * only, profit/loss mark data only, and every figure uses `num`.
 */

import { Checkbox as BaseCheckbox } from '@base-ui/react/checkbox'
import { mergeProps } from '@base-ui/react/merge-props'
import { Radio } from '@base-ui/react/radio'
import { RadioGroup } from '@base-ui/react/radio-group'
import { Switch as BaseSwitch } from '@base-ui/react/switch'
import { Toggle } from '@base-ui/react/toggle'
import { ToggleGroup } from '@base-ui/react/toggle-group'
import { useRender } from '@base-ui/react/use-render'
import { createLink } from '@tanstack/react-router'
import {
  CheckIcon,
  ChevronRightIcon,
  CircleAlertIcon,
  InfoIcon,
  LoaderCircleIcon,
  MinusIcon,
  TriangleAlertIcon,
} from 'lucide-react'
import { type ComponentProps, Fragment, type ReactNode, useId } from 'react'
import { ApiError, errorMessage } from '@/api/http'
import type { CheckResult } from '@/api/types'
import { cn } from '@/lib/cn'
import { type CORE_ORDER, isNum } from '@/lib/format'

// ── Tone helpers: data colour comes from the value, never from decoration ──────────────

export type Tone = 'neutral' | 'muted' | 'profit' | 'loss' | 'warn'

export const TEXT_TONE: Record<Tone, string> = {
  neutral: 'text-ink',
  muted: 'text-ink-subtle',
  profit: 'text-pnl-positive-text',
  loss: 'text-pnl-negative-text',
  warn: 'text-status-warning',
}

/** Sign of money made or lost: a return, a PnL, a change to a portfolio. */
export const signTone = (v: number | null | undefined): Tone =>
  v == null || v === 0 ? 'neutral' : v > 0 ? 'profit' : 'loss'

/**
 * A diagnostic ratio judged against the cutoff BRAIN submits at rather than against zero: red
 * when negative (an inverted signal), green once it `meets` the cutoff, and plain between, since
 * a positive figure is not yet a good one. With no cutoff to meet, only red is earned.
 */
export const cutoffTone = (v: number | null | undefined, meets = false): Tone =>
  v == null || Number.isNaN(v) ? 'neutral' : v < 0 ? 'loss' : meets ? 'profit' : 'neutral'

/** The limits BRAIN's checks set one Alpha's figures, by check name (`LOW_SHARPE`, …). */
export type Limits = Partial<Record<string, number>>

/**
 * A core figure's tone: Returns by sign; Sharpe, Fitness and Turnover against the limits the
 * Alpha's own checks name, so with none known only a negative is called out; Margin, which BRAIN
 * checks against nothing, only when negative; Drawdown not at all.
 */
export function figureTone(
  key: (typeof CORE_ORDER)[number],
  v: number | null | undefined,
  limits: Limits = {},
): Tone {
  if (!isNum(v)) return 'neutral'
  const at = (check: string) => limits[check]
  switch (key) {
    case 'returns':
      return signTone(v)
    case 'sharpe': {
      const floor = at('LOW_SHARPE')
      return cutoffTone(v, floor !== undefined && v >= floor)
    }
    case 'fitness': {
      const floor = at('LOW_FITNESS')
      return cutoffTone(v, floor !== undefined && v >= floor)
    }
    // A band rather than a floor: outside it the Alpha fails the check outright.
    case 'turnover': {
      const [low, high] = [at('LOW_TURNOVER'), at('HIGH_TURNOVER')]
      if ((low !== undefined && v < low) || (high !== undefined && v > high)) return 'loss'
      return low !== undefined && high !== undefined ? 'profit' : 'neutral'
    }
    case 'margin':
      return cutoffTone(v)
    case 'drawdown':
      return 'neutral'
  }
}

/**
 * One BRAIN submission check, coloured by what it says on its own: passed, unsettled, or
 * refused. ``PENDING`` is not green — nothing has been established yet — but it is not red
 * either, because BRAIN has not refused anything. Whether the Alpha as a whole counts as
 * submittable while a check is pending is a different question; see the Tasks results pane.
 */
export const checkTone = (result: CheckResult | null | undefined): Tone =>
  result === 'PASS'
    ? 'profit'
    : result === 'WARNING' || result === 'PENDING'
      ? 'warn'
      : result === 'FAIL' || result === 'ERROR'
        ? 'loss'
        : 'muted'

// ── Layout ──────────────────────────────────────────────────────────────────────────────

/** A screen's root: the canvas shows through the gaps as the gutter. */
export function Page({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn('flex min-w-0 flex-col gap-4 p-3 lg:p-4', className)}>{children}</div>
}

export function PageHeader({
  title,
  description,
  actions,
  sticky = false,
}: {
  title: ReactNode
  description?: ReactNode
  actions?: ReactNode
  /** Stays at the top while the page scrolls, for a form whose buttons are up here and whose
   *  last inputs are at the bottom. */
  sticky?: boolean
}) {
  return (
    <header
      className={cn(
        'flex flex-wrap items-end justify-between gap-3 px-1 pt-1',
        sticky &&
          '-mx-3 -mt-3 sticky top-0 z-20 border-b border-hairline bg-canvas/95 px-4 py-3 backdrop-blur-sm lg:-mx-4 lg:-mt-4 lg:px-5',
      )}
    >
      <div className="flex min-w-0 flex-col gap-1">
        <h1 className="text-headline text-balance break-words font-semibold text-ink">{title}</h1>
        {description && (
          <p className="max-w-3xl text-body text-pretty break-words text-ink-subtle">
            {description}
          </p>
        )}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </header>
  )
}

export function Panel({
  title,
  description,
  actions,
  children,
  className,
  bodyClassName,
}: {
  title?: ReactNode
  description?: ReactNode
  actions?: ReactNode
  children?: ReactNode
  className?: string
  bodyClassName?: string
}) {
  return (
    <section
      className={cn(
        'panel-highlight flex min-w-0 flex-col rounded-lg border border-hairline bg-surface-1',
        className,
      )}
    >
      {(title || actions) && (
        <header className="flex flex-wrap items-start justify-between gap-2 border-b border-hairline px-4 py-3">
          <div className="flex min-w-0 flex-col gap-0.5">
            {title && (
              <h2 className="text-title text-balance break-words font-medium text-ink">{title}</h2>
            )}
            {description && (
              <p className="text-body-compact text-pretty break-words text-ink-subtle">
                {description}
              </p>
            )}
          </div>
          {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={cn('min-w-0 p-4', bodyClassName)}>{children}</div>
    </section>
  )
}

/** Link text: lavender on dark, brightening to ink under the pointer. */
export const LINK = 'text-link underline underline-offset-2 transition-colors hover:text-ink'

// ── Actions ─────────────────────────────────────────────────────────────────────────────

/** DESIGN.md button-primary…button-danger. */
const BUTTON =
  'inline-flex shrink-0 items-center justify-center gap-1.5 font-medium whitespace-nowrap transition-colors select-none disabled:pointer-events-none disabled:bg-(--btn-disabled-bg) disabled:text-(--btn-disabled-text) aria-disabled:pointer-events-none aria-disabled:bg-(--btn-disabled-bg) aria-disabled:text-(--btn-disabled-text) [&_svg]:shrink-0'
const BUTTON_VARIANT = {
  primary:
    'bg-(--btn-primary-bg) text-(--btn-primary-text) hover:bg-(--btn-primary-bg-hover) active:bg-(--btn-primary-bg-active)',
  secondary:
    'border border-hairline bg-surface-2 text-ink hover:border-hairline-strong hover:bg-surface-3 active:bg-surface-4',
  ghost: 'text-ink-muted hover:bg-surface-2 hover:text-ink active:bg-surface-3',
  danger:
    'border border-hairline bg-surface-2 text-pnl-negative-text hover:border-(--btn-danger-border-hover) hover:bg-pnl-negative-tint active:border-pnl-negative',
}
const BUTTON_SIZE = {
  // The input's height, so a button beside a field lines up with it, border or none.
  md: 'h-8 rounded-sm px-3 text-body [&_svg]:size-4',
  sm: 'h-7 rounded-sm px-2 text-body-compact [&_svg]:size-3.5',
  icon: 'size-8 rounded-sm [&_svg]:size-4',
  'icon-sm': 'size-7 rounded-sm [&_svg]:size-3.5',
}

type ButtonProps = ComponentProps<'button'> & {
  variant?: keyof typeof BUTTON_VARIANT
  size?: keyof typeof BUTTON_SIZE
  loading?: boolean | undefined
  /** Render as another element with the button's look, e.g. `render={<a href={url} />}` or `render={<Link to="…" />}`. */
  render?: useRender.RenderProp
}

export function Button({
  variant = 'secondary',
  size = 'md',
  loading,
  render,
  className,
  children,
  disabled,
  type = 'button',
  ref,
  ...props
}: ButtonProps) {
  const inert = Boolean(disabled || loading)
  return useRender({
    defaultTagName: 'button',
    render,
    ref,
    props: mergeProps<'button'>(
      {
        // A rendered link cannot be `disabled`: aria-disabled plus pointer-events-none stops the
        // mouse, and dropping it out of the tab order stops Enter from following it anyway.
        type: render ? undefined : type,
        disabled: render ? undefined : inert,
        'aria-disabled': render && inert ? true : undefined,
        tabIndex: render && inert ? -1 : undefined,
        className: cn(BUTTON, BUTTON_VARIANT[variant], BUTTON_SIZE[size], className),
        children: (
          <>
            {loading && <LoaderCircleIcon className="animate-spin" />}
            {children}
          </>
        ),
      },
      props,
    ),
  })
}

// ── Inputs ──────────────────────────────────────────────────────────────────────────────

const FIELD =
  'w-full min-w-0 rounded-sm border border-(--field-border) hover:border-(--field-border-hover) focus-visible:border-(--field-border-hover) bg-surface-1 px-3 text-body text-ink placeholder:text-ink-subtle transition-colors disabled:border-(--field-border-disabled) disabled:text-(--field-text-disabled) aria-invalid:border-(--field-border-invalid)'

export function Input({ className, ...props }: ComponentProps<'input'>) {
  return (
    <input className={cn(FIELD, 'h-8', props.type === 'number' && 'num', className)} {...props} />
  )
}

export function Textarea({ className, ...props }: ComponentProps<'textarea'>) {
  return <textarea className={cn(FIELD, 'min-h-20 py-2', className)} {...props} />
}

export function Field({
  label,
  hint,
  error,
  children,
  className,
}: {
  label: ReactNode
  hint?: ReactNode
  /** Replaces the hint, inside the label so the control's name carries it; give the control `aria-invalid`. */
  error?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    // biome-ignore lint/a11y/noLabelWithoutControl: the control arrives as children
    <label className={cn('flex min-w-0 flex-col gap-1.5', className)}>
      <span className="text-caption font-medium text-ink-muted">{label}</span>
      {children}
      <FieldNote hint={hint} error={error} />
    </label>
  )
}

function FieldNote({ hint, error }: { hint?: ReactNode; error?: ReactNode }) {
  return error ? (
    <span role="alert" className="text-body-compact break-words text-pnl-negative-text">
      {error}
    </span>
  ) : (
    hint && <span className="text-body-compact text-pretty text-ink-subtle">{hint}</span>
  )
}

/** A Field for a group of controls (chips, a min–max pair): a legend names the group, since a label may wrap only one control. */
export function Fieldset({
  legend,
  hint,
  children,
  className,
}: {
  legend: ReactNode
  hint?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <fieldset className={cn('flex min-w-0 flex-col gap-1.5', className)}>
      <legend className="text-caption font-medium float-left w-full text-ink-muted">
        {legend}
      </legend>
      {children}
      <FieldNote hint={hint} />
    </fieldset>
  )
}

/**
 * A tick box that belongs to this app. The browser's own control paints itself from the OS
 * theme and ignores every token in `DESIGN.md`, so this is Base UI's headless root wearing
 * the same field tokens the inputs and selects wear.
 *
 * The label is tied on with `htmlFor` rather than wrapped around: Base UI renders a button,
 * and a `<label>` with no input inside it labels nothing.
 */
export function Checkbox({
  label,
  checked,
  indeterminate = false,
  onChange,
  disabled,
  className,
  ...props
}: {
  /** `null` for a box labelled by `aria-label` alone, as in a table row. */
  label: ReactNode
  checked: boolean
  /** Some of what it stands for is ticked, not all: drawn as a dash. */
  indeterminate?: boolean | undefined
  onChange: (checked: boolean) => void
  disabled?: boolean | undefined
  className?: string | undefined
  'aria-label'?: string | undefined
}) {
  const id = useId()
  return (
    <span className={cn('group/check inline-flex items-start gap-2.5', className)}>
      <BaseCheckbox.Root
        id={id}
        checked={checked}
        indeterminate={indeterminate}
        disabled={disabled}
        onCheckedChange={onChange}
        className={cn(
          'mt-px flex size-4 shrink-0 items-center justify-center rounded-xs border transition-colors',
          'border-(--field-border) bg-surface-1',
          'data-[checked]:border-(--btn-primary-bg) data-[checked]:bg-(--btn-primary-bg)',
          'data-[indeterminate]:border-(--btn-primary-bg) data-[indeterminate]:bg-(--btn-primary-bg)',
          'data-[disabled]:border-(--field-border-disabled) data-[disabled]:bg-surface-2',
          !disabled && 'cursor-pointer group-hover/check:border-(--field-border-hover)',
        )}
        {...props}
      >
        <BaseCheckbox.Indicator className="flex text-(--btn-primary-text)">
          {indeterminate ? (
            <MinusIcon className="size-3" strokeWidth={3.5} aria-hidden />
          ) : (
            <CheckIcon className="size-3" strokeWidth={3.5} aria-hidden />
          )}
        </BaseCheckbox.Indicator>
      </BaseCheckbox.Root>
      {label && (
        <label
          htmlFor={id}
          className={cn(
            'flex min-w-0 flex-col gap-0.5 text-body select-none',
            disabled
              ? 'cursor-not-allowed text-(--field-text-disabled)'
              : 'cursor-pointer text-ink-muted group-hover/check:text-ink',
          )}
        >
          {label}
        </label>
      )}
    </span>
  )
}

/** An on/off setting that takes effect at once, unlike a Checkbox, which is a choice in a form
 *  submitted later. On reads in the same lavender as a checked box. */
export function Switch({
  checked,
  onChange,
  disabled,
  ...props
}: {
  checked: boolean
  onChange: (checked: boolean) => void
  disabled?: boolean | undefined
  'aria-label'?: string | undefined
  'aria-labelledby'?: string | undefined
  'aria-describedby'?: string | undefined
}) {
  return (
    <BaseSwitch.Root
      checked={checked}
      disabled={disabled}
      onCheckedChange={onChange}
      className={cn(
        'relative inline-flex h-5 w-9 shrink-0 items-center rounded-pill border p-0.5 transition-colors',
        'border-(--field-border) bg-surface-3',
        'data-[checked]:border-(--btn-primary-bg) data-[checked]:bg-(--btn-primary-bg)',
        'data-[disabled]:border-(--field-border-disabled) data-[disabled]:opacity-60',
        !disabled && 'cursor-pointer hover:border-(--field-border-hover)',
      )}
      {...props}
    >
      <BaseSwitch.Thumb className="size-3.5 rounded-pill bg-ink-muted transition-transform data-[checked]:translate-x-4 data-[checked]:bg-(--btn-primary-text)" />
    </BaseSwitch.Root>
  )
}

/** Multi-choice as toggle chips (Base UI ToggleGroup: pressed state, arrow-key focus). A pressed chip lifts to surface-3. */
export function Chips<V extends string>({
  items,
  value,
  onChange,
  label,
  disabled,
  size = 'md',
}: {
  items: { value: V; label: ReactNode; title?: string | undefined }[]
  value: V[]
  onChange: (value: V[]) => void
  label: string
  disabled?: boolean | undefined
  /** `lg` lays a few important choices out as tiles that share the row between them. */
  size?: 'md' | 'lg'
}) {
  return (
    <ToggleGroup
      multiple
      aria-label={label}
      value={value}
      disabled={disabled}
      onValueChange={(next) => onChange(next as V[])}
      className={
        size === 'lg'
          ? 'grid grid-cols-[repeat(auto-fit,minmax(11rem,1fr))] gap-2'
          : 'flex flex-wrap gap-1.5'
      }
    >
      {items.map((item) => (
        <Toggle
          key={item.value}
          value={item.value}
          title={item.title}
          className={cn(
            'rounded-sm border whitespace-nowrap border-(--field-border) bg-surface-1 text-ink-muted transition-colors hover:border-(--field-border-hover) hover:text-ink data-[disabled]:border-(--field-border-disabled) data-[disabled]:text-(--field-text-disabled) data-[pressed]:border-primary data-[pressed]:bg-primary-subtle data-[pressed]:text-ink',
            size === 'lg' ? 'h-11 px-4 text-body' : 'h-7 px-2 text-body-compact',
          )}
        >
          {item.label}
        </Toggle>
      ))}
    </ToggleGroup>
  )
}

/** One choice among a few, as a segmented control (Base UI RadioGroup: radio semantics, arrow-key focus). */
export function Segmented<V extends string | number>({
  items,
  value,
  onChange,
  label,
}: {
  items: { value: V; label: ReactNode; disabled?: boolean }[]
  value: V
  onChange: (value: V) => void
  label: string
}) {
  return (
    <RadioGroup
      aria-label={label}
      value={String(value)}
      // Base UI values are strings; map back so numeric choices stay numbers.
      onValueChange={(next) => {
        const item = items.find((i) => String(i.value) === String(next))
        if (item) onChange(item.value)
      }}
      className="inline-flex flex-wrap items-center gap-0.5 rounded-sm border border-(--field-border) bg-surface-1 p-0.5"
    >
      {items.map((item) => (
        <Radio.Root
          key={String(item.value)}
          value={String(item.value)}
          disabled={item.disabled}
          className="inline-flex h-7 items-center justify-center rounded-xs border border-transparent px-3 text-body-compact leading-none font-medium text-ink-subtle transition-colors hover:text-ink data-[checked]:border-hairline-strong data-[checked]:bg-surface-3 data-[checked]:text-ink data-[disabled]:text-ink-disabled data-[disabled]:hover:text-ink-disabled"
        >
          {item.label}
        </Radio.Root>
      ))}
    </RadioGroup>
  )
}

// ── Data display ────────────────────────────────────────────────────────────────────────

/** Status that is not clickable: a neutral outline, a subtle label and `num text-ink` figures, never a hue. */
export const STATUS =
  'flex h-7 items-center gap-1.5 whitespace-nowrap rounded-xs border border-hairline-strong px-2 text-body-compact text-ink-subtle'

export function Metric({
  label,
  value,
  hint,
  tone = 'neutral',
  size = 'md',
  boxed,
}: {
  label: ReactNode
  value: ReactNode
  hint?: ReactNode
  tone?: Tone
  size?: 'sm' | 'md'
  /** Outlined like the STATUS boxes in the top bar. */
  boxed?: boolean
}) {
  return (
    <div
      className={cn(
        'flex min-w-0 flex-col gap-1.5',
        boxed &&
          'rounded-md border border-hairline-strong bg-surface-2 px-3 py-2 transition-colors',
      )}
    >
      <span className="text-caption font-medium uppercase tracking-wide text-ink-subtle">
        {label}
      </span>
      <span
        className={cn(
          size === 'md' ? 'num text-headline leading-none font-semibold' : 'mono-metric',
          TEXT_TONE[tone],
        )}
      >
        {value}
      </span>
      {hint && <span className="text-body-compact text-ink-subtle">{hint}</span>}
    </div>
  )
}

const BADGE: Record<Tone | 'outline', string> = {
  neutral: 'bg-surface-3 text-ink-muted',
  muted: 'bg-surface-2 text-ink-subtle',
  profit: 'text-pnl-positive-text',
  loss: 'text-pnl-negative-text',
  warn: 'text-status-warning',
  outline: 'border border-hairline-strong text-ink-subtle',
}

export function Badge({
  tone = 'neutral',
  children,
  className,
  title,
}: {
  tone?: Tone | 'outline' | undefined
  children: ReactNode
  className?: string
  title?: string
}) {
  return (
    <span
      title={title}
      className={cn(
        'mono-metric inline-flex max-w-full min-w-0 items-center gap-1 overflow-hidden rounded-xs px-1.5 py-0.5 whitespace-nowrap',
        BADGE[tone],
        className,
      )}
    >
      {children}
    </span>
  )
}

const METRIC_BADGE = {
  profit: 'bg-pnl-positive-tint text-pnl-positive-text border border-pnl-positive-edge',
  loss: 'bg-pnl-negative-tint text-pnl-negative-text border border-pnl-negative-edge',
  neutral: 'bg-surface-2 text-ink border border-hairline',
}

export function MetricBadge({
  tone = 'neutral',
  children,
}: {
  tone?: keyof typeof METRIC_BADGE
  children: ReactNode
}) {
  return (
    <span
      className={cn(
        'mono-metric inline-flex items-center gap-1 rounded-xs px-1.5 py-0.5 whitespace-nowrap',
        METRIC_BADGE[tone],
      )}
    >
      {children}
    </span>
  )
}

export function KV({ items, className }: { items: [ReactNode, ReactNode][]; className?: string }) {
  return (
    <dl
      className={cn('grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1.5 text-body', className)}
    >
      {items.map(([k, v], i) => (
        <Fragment key={i}>
          <dt className="text-ink-subtle">{k}</dt>
          <dd className="num min-w-0 truncate text-ink">{v}</dd>
        </Fragment>
      ))}
    </dl>
  )
}

export function Progress({
  value,
  className,
  label,
  mark,
}: {
  value: number | null
  className?: string
  label?: string
  /** A tick across the track at this fraction, for a fill to be read against: the quota gauge's
   *  share of the day gone. */
  mark?: { at: number; label: string } | undefined
}) {
  const clamped = value == null ? null : Math.min(1, Math.max(0, value))
  const bar = (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={1}
      aria-valuenow={clamped ?? undefined}
      className={cn('h-1.5 overflow-hidden rounded-pill bg-surface-2', className)}
    >
      <div
        // Determinate progress scales rather than resizes, so the fill animates on the compositor.
        className={cn(
          'h-full rounded-pill bg-primary',
          clamped == null ? 'w-1/3 animate-pulse' : 'origin-left transition-transform',
        )}
        style={clamped == null ? undefined : { transform: `scaleX(${clamped})` }}
      />
    </div>
  )
  if (!mark) return bar
  return (
    <div className="relative">
      {bar}
      <span
        role="img"
        aria-label={mark.label}
        title={mark.label}
        className="absolute top-1/2 h-2.5 w-0.5 -translate-x-1/2 -translate-y-1/2 rounded-pill bg-ink-muted"
        style={{ left: `${Math.min(1, Math.max(0, mark.at)) * 100}%` }}
      />
    </div>
  )
}

export function Kbd({ children }: { children: ReactNode }) {
  return (
    <kbd className="rounded-xs border border-hairline-strong bg-surface-2 px-1 text-caption leading-4 text-ink-subtle">
      {children}
    </kbd>
  )
}

// ── Feedback ────────────────────────────────────────────────────────────────────────────

export function Spinner({ className }: { className?: string }) {
  return (
    <LoaderCircleIcon
      aria-label="Loading"
      className={cn('size-4 animate-spin text-ink-subtle', className)}
    />
  )
}

/** A placeholder shape. Give one `label` per loading region so screen readers hear it too. */
export function Skeleton({ className, label }: { className?: string; label?: string }) {
  return (
    <>
      <div aria-hidden className={cn('animate-pulse rounded-md bg-surface-2', className)} />
      {label && (
        <span role="status" className="sr-only">
          {label}
        </span>
      )}
    </>
  )
}

const NOTICE = {
  info: {
    border: 'border-hairline-strong',
    icon: InfoIcon,
    color: 'text-ink-subtle',
  },
  warn: {
    border: 'border-status-warning-edge',
    icon: TriangleAlertIcon,
    color: 'text-status-warning',
  },
  error: {
    border: 'border-pnl-negative-edge',
    icon: CircleAlertIcon,
    color: 'text-pnl-negative-text',
  },
}

/** Never under-deliver silently: every refusal and shortfall is said in one of these. */
export function Notice({
  tone = 'info',
  title,
  children,
  action,
  className,
}: {
  tone?: keyof typeof NOTICE
  title?: ReactNode
  children?: ReactNode
  action?: ReactNode
  className?: string | undefined
}) {
  const { border, icon: Icon, color } = NOTICE[tone]
  return (
    <div
      role={tone === 'error' ? 'alert' : 'status'}
      className={cn(
        'flex gap-3 rounded-md border bg-surface-2 px-3 py-2 text-body',
        border,
        className,
      )}
    >
      <Icon className={cn('mt-0.5 size-4 shrink-0', color)} aria-hidden />
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        {title && <p className="font-medium text-balance break-words text-ink">{title}</p>}
        {children && <div className="text-pretty break-words text-ink-muted">{children}</div>}
      </div>
      {action}
    </div>
  )
}

/** A thrown query/mutation error, with BRAIN's verification link when it asks for one. */
export function ErrorNotice({
  error,
  title = 'That did not work',
  className,
}: {
  error: unknown
  title?: ReactNode
  className?: string
}) {
  const url = error instanceof ApiError ? error.body.verificationUrl : undefined
  return (
    <Notice tone="error" title={title} className={className}>
      {errorMessage(error)}{' '}
      {url && (
        <a href={url} target="_blank" rel="noopener noreferrer" className={LINK}>
          Open the verification page
        </a>
      )}
    </Notice>
  )
}

export function Empty({
  title,
  children,
  icon,
  className,
}: {
  title: ReactNode
  children?: ReactNode
  icon?: ReactNode
  className?: string
}) {
  return (
    <div
      className={cn(
        'flex flex-col items-center justify-center gap-2 px-6 py-10 text-center',
        className,
      )}
    >
      {icon && <div className="text-ink-tertiary [&_svg]:size-5">{icon}</div>}
      <p role="status" className="text-body font-medium text-balance break-words text-ink">
        {title}
      </p>
      {children && <div className="text-body-compact text-pretty text-ink-subtle">{children}</div>}
    </div>
  )
}

/** Advanced controls stay behind a one-click triangle (CLAUDE.md anti-goal 2). */
export function Disclosure({
  summary,
  children,
  defaultOpen,
  className,
}: {
  summary: ReactNode
  children: ReactNode
  defaultOpen?: boolean | undefined
  className?: string
}) {
  return (
    <details
      open={defaultOpen}
      className={cn('group rounded-md border border-hairline', className)}
    >
      <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 text-body text-ink-muted select-none hover:text-ink [&::-webkit-details-marker]:hidden">
        <ChevronRightIcon
          className="size-3.5 shrink-0 transition-transform group-open:rotate-90"
          aria-hidden
        />
        {summary}
      </summary>
      <div className="border-t border-hairline bg-surface-1 px-4 py-3">{children}</div>
    </details>
  )
}

// ── Navigation ──────────────────────────────────────────────────────────────────────────

function TabAnchor({ className, ...props }: ComponentProps<'a'>) {
  return (
    <a
      {...props}
      className={cn(
        'relative inline-flex h-9 shrink-0 items-center text-body text-ink-subtle transition-colors hover:text-ink focus-visible:-outline-offset-2',
        'after:absolute after:inset-x-0 after:bottom-0 after:h-px data-[status=active]:text-ink data-[status=active]:after:bg-primary',
        className,
      )}
    />
  )
}

/** A sub-tab that is a real link: `<TabLink to="/pool/$tab" params={{ tab: 'stored' }}>`. */
export const TabLink = createLink(TabAnchor)

export function TabBar({ children }: { children: ReactNode }) {
  return (
    <nav className="flex gap-5 overflow-x-auto overflow-y-hidden border-b border-hairline px-1">
      {children}
    </nav>
  )
}
