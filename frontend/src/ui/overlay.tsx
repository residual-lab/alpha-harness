/**
 * Floating surfaces on Base UI's headless primitives (focus trap, dismissal, ARIA), styled
 * as lifted Linear surfaces: surface-3/4 with a strong hairline, casting `shadow-float`
 * onto whatever they cover.
 */

import { ContextMenu as BContextMenu } from '@base-ui/react/context-menu'
import { Dialog as BDialog } from '@base-ui/react/dialog'
import { Menu as BMenu } from '@base-ui/react/menu'
import { Select as BSelect } from '@base-ui/react/select'
import { Tooltip as BTooltip } from '@base-ui/react/tooltip'
import { CheckIcon, ChevronDownIcon, ChevronUpIcon, XIcon } from 'lucide-react'
import { type ReactElement, type ReactNode, type RefObject, useEffect } from 'react'
import { cn } from '@/lib/cn'
import type { Choice } from '@/lib/scope'
import { Button } from './kit'

/** Behind every modal: the page dims and blurs, so what floats is the only thing in focus. */
export const BACKDROP = 'fixed inset-0 z-50 bg-black/60 backdrop-blur-sm'
/** Lighter, and unblurred: a sheet is read against the table it was opened from. */
const SHEET_BACKDROP = 'fixed inset-0 z-50 bg-black/25'

interface DialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: ReactNode
  description?: ReactNode
  children?: ReactNode
  footer?: ReactNode
  /** Buttons beside Close in the header. */
  actions?: ReactNode
  className?: string
  /**
   * Where to portal to. Needed only inside an element in fullscreen: the browser renders
   * that element and its descendants and nothing else, so a popup left on `<body>` is
   * simply not drawn.
   */
  container?: RefObject<HTMLElement | null> | undefined
}

function Frame({
  title,
  description,
  children,
  footer,
  actions,
}: Omit<DialogProps, 'open' | 'onOpenChange' | 'className'>) {
  return (
    <>
      <div className="flex items-start justify-between gap-4 border-b border-hairline px-4 py-3">
        <div className="flex min-w-0 flex-col gap-0.5">
          <BDialog.Title className="text-title text-balance break-words">{title}</BDialog.Title>
          {description && (
            <BDialog.Description className="text-body-compact text-pretty break-words text-ink-subtle">
              {description}
            </BDialog.Description>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {actions}
          <BDialog.Close render={<Button variant="ghost" size="icon-sm" aria-label="Close" />}>
            <XIcon />
          </BDialog.Close>
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-4">{children}</div>
      {footer && (
        <div className="flex flex-wrap justify-end gap-2 border-t border-hairline px-4 py-3">
          {footer}
        </div>
      )}
    </>
  )
}

export function Dialog({ open, onOpenChange, className, ...frame }: DialogProps) {
  return (
    <BDialog.Root open={open} onOpenChange={(next) => onOpenChange(next)}>
      <BDialog.Portal>
        <BDialog.Backdrop className={BACKDROP} />
        <BDialog.Popup
          className={cn(
            'fixed top-1/2 left-1/2 z-50 flex max-h-[85vh] w-[calc(100%-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 flex-col rounded-lg border border-hairline-strong bg-surface-3 shadow-float outline-none',
            className,
          )}
        >
          <Frame {...frame} />
        </BDialog.Popup>
      </BDialog.Portal>
    </BDialog.Root>
  )
}

/** A right-hand detail panel. */
export function Sheet({ open, onOpenChange, className, container, ...frame }: DialogProps) {
  return (
    <BDialog.Root open={open} onOpenChange={(next) => onOpenChange(next)}>
      <BDialog.Portal container={container}>
        <BDialog.Backdrop className={SHEET_BACKDROP} />
        <BDialog.Popup
          className={cn(
            'fixed inset-y-0 right-0 z-50 flex w-full max-w-2xl flex-col border-l border-hairline-strong bg-surface-1 shadow-float outline-none',
            className,
          )}
        >
          <Frame {...frame} />
        </BDialog.Popup>
      </BDialog.Portal>
    </BDialog.Root>
  )
}

/**
 * Previous and next for a sheet opened from a table's rows, `ids` in the order shown: on the
 * buttons, and on K and J as in a mail list (not while typing or in a menu, where they are letters).
 */
export function SheetSteps({
  ids,
  id,
  onStep,
  noun,
}: {
  ids: string[]
  id: string | null
  onStep: (id: string) => void
  noun: string
}) {
  const at = id === null ? -1 : ids.indexOf(id)
  const previous = at > 0 ? ids[at - 1] : undefined
  const next = at >= 0 ? ids[at + 1] : undefined
  useEffect(() => {
    if (at < 0) return
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return
      const target = event.target as HTMLElement | null
      // Nor inside an open menu or list, where a letter jumps to the item it starts.
      if (
        target?.closest(
          'input, textarea, select, [contenteditable="true"], [role="menu"], [role="listbox"], [role="combobox"]',
        )
      )
        return
      const to = event.key === 'j' ? next : event.key === 'k' ? previous : undefined
      if (to) onStep(to)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [at, next, previous, onStep])

  if (at < 0 || ids.length < 2) return null
  return (
    <>
      <span className="num px-1 text-body-compact text-ink-subtle">
        {at + 1} / {ids.length}
      </span>
      <Button
        variant="ghost"
        size="icon-sm"
        aria-label={`Previous ${noun} (K)`}
        title={`Previous ${noun} (K)`}
        disabled={!previous}
        onClick={() => previous && onStep(previous)}
      >
        <ChevronUpIcon />
      </Button>
      <Button
        variant="ghost"
        size="icon-sm"
        aria-label={`Next ${noun} (J)`}
        title={`Next ${noun} (J)`}
        disabled={!next}
        onClick={() => next && onStep(next)}
      >
        <ChevronDownIcon />
      </Button>
    </>
  )
}

/** Anything that spends quota or asks BRAIN to cancel goes through one of these. */
export function Confirm({
  open,
  onOpenChange,
  title,
  children,
  confirmLabel,
  cancelLabel = 'Cancel',
  onConfirm,
  danger,
  pending,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: ReactNode
  children?: ReactNode
  confirmLabel: ReactNode
  cancelLabel?: ReactNode
  onConfirm: () => void
  danger?: boolean
  pending?: boolean
}) {
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={title}
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            {cancelLabel}
          </Button>
          <Button variant={danger ? 'danger' : 'primary'} loading={pending} onClick={onConfirm}>
            {confirmLabel}
          </Button>
        </>
      }
    >
      <div className="text-body text-ink-muted">{children}</div>
    </Dialog>
  )
}

const POPUP =
  'max-w-[calc(100vw-2rem)] rounded-md border border-hairline-strong bg-surface-3 p-1 shadow-float outline-none'
const ITEM =
  'flex h-8 min-w-0 cursor-default items-center gap-2 rounded-sm px-2 text-body text-ink-muted outline-none select-none data-[disabled]:text-ink-disabled data-[highlighted]:bg-surface-4 data-[highlighted]:text-ink'

export function Select<V extends string>({
  value,
  onChange,
  items,
  label,
  className,
  mono,
  disabled,
}: {
  value: V | null
  onChange: (value: V) => void
  items: Choice<V>[]
  label: string
  className?: string
  mono?: boolean
  disabled?: boolean | undefined
}) {
  return (
    <BSelect.Root
      items={items}
      value={value}
      disabled={disabled}
      onValueChange={(next) => next != null && onChange(next as V)}
    >
      <BSelect.Trigger
        aria-label={label}
        className={cn(
          'inline-flex h-8 max-w-full min-w-0 items-center justify-between gap-2 rounded-sm border border-(--field-border) hover:border-(--field-border-hover) focus-visible:border-(--field-border-hover) bg-surface-1 px-3 text-body text-ink transition-colors active:bg-surface-2 data-[disabled]:border-(--field-border-disabled) data-[disabled]:text-(--field-text-disabled) data-[popup-open]:border-(--field-border-open)',
          mono && 'num',
          className,
        )}
      >
        <BSelect.Value className="truncate" />
        <BSelect.Icon className="shrink-0 text-ink-subtle">
          <ChevronDownIcon className="size-3.5" />
        </BSelect.Icon>
      </BSelect.Trigger>
      <BSelect.Portal>
        <BSelect.Positioner sideOffset={4} alignItemWithTrigger={false} className="z-50">
          <BSelect.Popup
            className={cn(
              POPUP,
              'max-h-(--available-height) min-w-(--anchor-width) overflow-y-auto',
            )}
          >
            <BSelect.List>
              {items.map((item) => (
                <BSelect.Item
                  key={item.value}
                  value={item.value}
                  className={cn(ITEM, 'justify-between', mono && 'num')}
                >
                  <BSelect.ItemText className="truncate">{item.label}</BSelect.ItemText>
                  <BSelect.ItemIndicator className="shrink-0 text-primary">
                    <CheckIcon className="size-3.5" />
                  </BSelect.ItemIndicator>
                </BSelect.Item>
              ))}
            </BSelect.List>
          </BSelect.Popup>
        </BSelect.Positioner>
      </BSelect.Portal>
    </BSelect.Root>
  )
}

interface MenuItem {
  label: ReactNode
  onClick: () => void
  icon?: ReactNode
  /** Shown at the row's right edge, as plain text. */
  shortcut?: string
  danger?: boolean
  disabled?: boolean
}

export function Menu({
  trigger,
  items,
  align = 'end',
}: {
  trigger: ReactElement
  items: MenuItem[]
  align?: 'start' | 'center' | 'end'
}) {
  return (
    <BMenu.Root>
      <BMenu.Trigger render={trigger} />
      <BMenu.Portal>
        <BMenu.Positioner sideOffset={4} align={align} className="z-50">
          <BMenu.Popup className={cn(POPUP, 'min-w-44')}>
            <MenuItems items={items} />
          </BMenu.Popup>
        </BMenu.Positioner>
      </BMenu.Portal>
    </BMenu.Root>
  )
}

function MenuItems({ items }: { items: MenuItem[] }) {
  return items.map((item, i) => (
    <BMenu.Item
      key={i}
      disabled={item.disabled}
      onClick={item.onClick}
      className={cn(
        ITEM,
        item.danger && 'text-pnl-negative-text data-[highlighted]:text-pnl-negative-text',
        '[&_svg]:size-3.5 [&_svg]:shrink-0',
      )}
    >
      {item.icon}
      <span className="truncate">{item.label}</span>
      {item.shortcut && (
        <span className="ml-auto shrink-0 pl-6 text-ink-subtle">{item.shortcut}</span>
      )}
    </BMenu.Item>
  ))
}

/** The same items as {@link Menu}, opened by a right-click (or a long press) on `children`. */
export function ContextMenu({ children, items }: { children: ReactElement; items: MenuItem[] }) {
  return (
    <BContextMenu.Root>
      <BContextMenu.Trigger render={children} />
      <BContextMenu.Portal>
        <BContextMenu.Positioner className="z-50">
          <BContextMenu.Popup className={cn(POPUP, 'min-w-44')}>
            <MenuItems items={items} />
          </BContextMenu.Popup>
        </BContextMenu.Positioner>
      </BContextMenu.Portal>
    </BContextMenu.Root>
  )
}

export function Tooltip({ content, children }: { content: ReactNode; children: ReactElement }) {
  return (
    <BTooltip.Root>
      <BTooltip.Trigger render={children} />
      <BTooltip.Portal>
        <BTooltip.Positioner sideOffset={6} className="z-50">
          <BTooltip.Popup className="max-w-[min(24rem,calc(100vw-2rem))] rounded-xs border border-hairline-strong bg-surface-4 px-2 py-1 text-caption shadow-float text-pretty break-words text-ink">
            {content}
          </BTooltip.Popup>
        </BTooltip.Positioner>
      </BTooltip.Portal>
    </BTooltip.Root>
  )
}
