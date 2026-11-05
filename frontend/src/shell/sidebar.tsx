/**
 * Navigation: the six areas, a live count of submittable Alphas, and the account menu.
 * Collapses to icons below `lg`.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Link, useRouterState } from '@tanstack/react-router'
import {
  CheckIcon,
  ChevronsUpDownIcon,
  ExternalLinkIcon,
  LogOutIcon,
  MessageSquareTextIcon,
  PanelLeftIcon,
  PowerIcon,
  SettingsIcon,
  ShieldCheckIcon,
} from 'lucide-react'
import { Fragment, useEffect, useState } from 'react'
import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { auth } from '@/api/core'
import { PLATFORM_URL } from '@/api/platform'
import type { Today } from '@/api/types'
import { cn } from '@/lib/cn'
import { Button, Empty, Kbd } from '@/ui/kit'
import { Confirm, Dialog, Menu, Tooltip } from '@/ui/overlay'
import { MOD } from './desktop'
import { NAV } from './nav'
import { SETTINGS_SHORTCUT, useSettings } from './settings'
import { UpdateBadge, useQuitApp, useUpdateStatus, VersionBadge } from './update'

/** GitHub's own chooser: a bug, a feature, or a security report. */
const FEEDBACK_URL = 'https://github.com/residual-lab/alpha-harness/issues/new/choose'

/** Areas a new consultant has to open once: Data (download fields) and AI (add a key). They flash until visited. */
const ONBOARDING: readonly string[] = ['data', 'ai']

const useVisited = create<{
  visited: string[]
  visit: (area: string) => void
}>()(
  persist(
    (set) => ({
      visited: [],
      visit: (area) =>
        set((s) => (s.visited.includes(area) ? s : { visited: [...s.visited, area] })),
    }),
    { name: 'alpha-harness-onboarding' },
  ),
)

/** Pending close of a peek, so crossing from the button onto the panel does not flicker it shut. */
let closing: ReturnType<typeof setTimeout> | undefined
const PEEK_CLOSE_MS = 200

/** The docked sidebar's width until it is first measured. */
export const SIDEBAR_WIDTH = 248

/**
 * Whether the sidebar is hidden altogether, rail included, and the width it docks at, both kept
 * across reloads; and whether a hidden one is peeking out under the pointer, which is not. The
 * peek is drawn at the docked width, so showing it moves nothing but the page beside it.
 */
export const useSidebarHidden = create<{
  hidden: boolean
  peek: boolean
  width: number
  setWidth: (width: number) => void
  toggle: () => void
  openPeek: () => void
  closePeek: () => void
}>()(
  persist(
    (set) => ({
      hidden: false,
      peek: false,
      width: SIDEBAR_WIDTH,
      setWidth: (width) => set({ width }),
      toggle: () => {
        clearTimeout(closing)
        set((s) => ({ hidden: !s.hidden, peek: false }))
      },
      openPeek: () => {
        clearTimeout(closing)
        set((s) => (s.hidden ? { peek: true } : s))
      },
      closePeek: () => {
        clearTimeout(closing)
        closing = setTimeout(() => set({ peek: false }), PEEK_CLOSE_MS)
      },
    }),
    { name: 'alpha-harness-sidebar', partialize: (s) => ({ hidden: s.hidden, width: s.width }) },
  ),
)

export const SIDEBAR_SHORTCUT = `${MOD}B`

/** Shows or hides the sidebar. Lives in the header, so it stays put whichever it is. */
export function SidebarToggle() {
  const hidden = useSidebarHidden((s) => s.hidden)
  const toggle = useSidebarHidden((s) => s.toggle)
  const openPeek = useSidebarHidden((s) => s.openPeek)
  const closePeek = useSidebarHidden((s) => s.closePeek)
  const label = hidden ? 'Show sidebar' : 'Hide sidebar'
  return (
    <Tooltip
      content={
        <span className="flex items-center gap-2">
          {label} <Kbd>{SIDEBAR_SHORTCUT}</Kbd>
        </span>
      }
    >
      <Button
        size="icon-sm"
        variant="ghost"
        aria-label={label}
        aria-expanded={!hidden}
        onClick={toggle}
        // Hovered while hidden, the sidebar peeks out over the page; a click pins it.
        onMouseEnter={openPeek}
        onMouseLeave={closePeek}
      >
        <PanelLeftIcon />
      </Button>
    </Tooltip>
  )
}

export function Sidebar({
  you,
  collapsed,
  onMouseEnter,
  onMouseLeave,
}: {
  you: Today['you']
  collapsed: boolean
  /** For a hidden sidebar peeking out, which stays open while the pointer is over it. */
  onMouseEnter?: () => void
  onMouseLeave?: () => void
}) {
  const queryClient = useQueryClient()
  const area = useRouterState({
    select: (s) => s.location.pathname.split('/')[1] ?? '',
  })
  const visited = useVisited((s) => s.visited)
  const visit = useVisited((s) => s.visit)
  // Reaching the area any way counts (sidebar, ⌘K or a link), so the flash never outlives the visit.
  useEffect(() => {
    if (ONBOARDING.includes(area)) visit(area)
  }, [area, visit])
  const [quitting, setQuitting] = useState(false)
  const [permissions, setPermissions] = useState(false)
  const quit = useQuitApp()
  const launched = useUpdateStatus().data?.launcher != null

  const signOut = useMutation({
    mutationFn: () => auth.logout(),
    onSuccess: async () => {
      // Dropped, not invalidated: invalidating would refetch every mounted screen against a
      // dead session, and leave the last account's data in the cache behind the sign-in screen.
      // Today is kept and refetched: the shell observes it, and clearing it would leave the
      // shell watching a query nothing refetches, so the workspace stayed on screen.
      queryClient.removeQueries({ predicate: (query) => query.queryKey[0] !== 'today' })
      await queryClient.refetchQueries({ queryKey: ['today'] })
    },
  })

  const name = you.fullName ?? you.userId ?? 'Signed in'
  const initials = name
    .split(/\s+/)
    .map((part) => part[0])
    .join('')
    .slice(0, 2)
    .toUpperCase()

  return (
    // A step up from the canvas, so the sidebar reads as its own column without a rule inside it.
    <aside
      className="flex h-full min-h-0 flex-col border-r border-hairline bg-surface-1"
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
    >
      <nav
        aria-label="Main"
        className="flex flex-1 flex-col gap-0.5 overflow-y-auto px-2 pt-2 pb-3"
      >
        {NAV.map((item, i) => (
          <Fragment key={item.to}>
            {item.group &&
              item.group !== NAV[i - 1]?.group &&
              // A heading on the wide sidebar; on the rail, where it cannot fit, a rule.
              (collapsed ? (
                <hr className="mx-2 my-2 border-hairline" />
              ) : (
                <div className="px-2 pt-4 pb-1 text-caption font-medium text-ink-subtle">
                  {item.group}
                </div>
              ))}
            <Link
              to={item.to}
              title={item.label}
              {...(collapsed && { 'aria-label': item.label })}
              className={cn(
                'group flex h-8 items-center gap-3 rounded-md text-body text-ink-muted transition-colors hover:bg-surface-3 hover:text-ink data-[status=active]:bg-surface-4 data-[status=active]:font-medium data-[status=active]:text-ink',
                collapsed ? 'justify-center' : 'px-2',
                ONBOARDING.includes(item.to.slice(1)) &&
                  !visited.includes(item.to.slice(1)) &&
                  'animate-attention text-status-warning motion-reduce:bg-status-warning-tint',
              )}
            >
              <item.icon className="size-4 shrink-0" aria-hidden />
              {!collapsed && <span className="flex-1 truncate">{item.label}</span>}
            </Link>
          </Fragment>
        ))}
      </nav>

      <div className="flex flex-col gap-1 border-t border-hairline p-2">
        <a
          href={FEEDBACK_URL}
          target="_blank"
          rel="noopener noreferrer"
          title="Give Feedback"
          {...(collapsed && { 'aria-label': 'Give Feedback' })}
          className={cn(
            'flex h-8 items-center gap-3 rounded-md text-body text-ink-muted transition-colors hover:bg-surface-3 hover:text-ink',
            collapsed ? 'justify-center' : 'px-2',
          )}
        >
          <MessageSquareTextIcon className="size-4 shrink-0" aria-hidden />
          {!collapsed && <span className="flex-1 truncate">Give Feedback</span>}
        </a>
        {/* Both above the account, where someone reading a screenshot looks for them. The
            update stays on the rail as an icon — it is the one thing here worth interrupting
            for — while the version hides: 52px has no room for a date. */}
        <UpdateBadge collapsed={collapsed} />
        {!collapsed && (
          <div className="px-1.5 pt-0.5 pb-1">
            <VersionBadge />
          </div>
        )}
        <Menu
          align="start"
          trigger={
            <button
              type="button"
              aria-label={`Account: ${name}`}
              className={cn(
                'flex h-11 w-full items-center gap-2.5 rounded-md text-left transition-colors hover:bg-surface-3 active:bg-surface-4 data-[popup-open]:bg-surface-4',
                collapsed ? 'justify-center' : 'px-1.5',
              )}
            >
              <span className="num flex size-7 shrink-0 items-center justify-center rounded-pill bg-surface-4 text-caption font-medium text-ink-muted">
                {initials}
              </span>
              {!collapsed && (
                <>
                  <span className="flex min-w-0 flex-1 flex-col leading-tight">
                    <span className="truncate text-body font-medium text-ink">{name}</span>
                    <span className="truncate text-caption text-ink-subtle">
                      {you.email ?? you.userId}
                    </span>
                  </span>
                  {/* Says it opens, which a name and an address alone do not. */}
                  <ChevronsUpDownIcon className="size-4 shrink-0 text-ink-subtle" aria-hidden />
                </>
              )}
            </button>
          }
          items={[
            {
              label: 'Settings',
              icon: <SettingsIcon />,
              shortcut: SETTINGS_SHORTCUT,
              onClick: () => useSettings.getState().setOpen(true),
            },
            {
              label: 'Permissions',
              icon: <ShieldCheckIcon />,
              onClick: () => setPermissions(true),
            },
            {
              label: 'Open the BRAIN Platform',
              icon: <ExternalLinkIcon />,
              onClick: () => window.open(PLATFORM_URL, '_blank', 'noopener,noreferrer'),
            },
            {
              label: 'Sign Out',
              icon: <LogOutIcon />,
              danger: true,
              disabled: signOut.isPending,
              onClick: () => signOut.mutate(),
            },
            // Only a launcher can bring the app back, so only a launched app offers to close.
            ...(launched
              ? [
                  {
                    label: 'Quit Alpha Harness',
                    icon: <PowerIcon />,
                    onClick: () => setQuitting(true),
                  },
                ]
              : []),
          ]}
        />
      </div>
      <Dialog
        open={permissions}
        onOpenChange={setPermissions}
        // Wide enough for the longest code BRAIN sends to sit whole in its tile.
        className="max-w-2xl"
        title="Permissions"
        description={`${you.permissions.length} granted to this account, as BRAIN names them.`}
      >
        {you.permissions.length ? (
          <ul className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {you.permissions.map((code) => (
              <li
                key={code}
                title={code}
                className="flex min-w-0 items-center gap-2 rounded-md border border-hairline bg-surface-2 px-3 py-2"
              >
                <CheckIcon className="size-3.5 shrink-0 text-pnl-positive-text" aria-hidden />
                <span className="num min-w-0 truncate text-body-compact text-ink">{code}</span>
              </li>
            ))}
          </ul>
        ) : (
          <Empty title="BRAIN reported no permissions for this account" />
        )}
      </Dialog>
      <Confirm
        open={quitting}
        onOpenChange={setQuitting}
        title="Quit Alpha Harness?"
        confirmLabel="Quit"
        pending={quit.isPending}
        onConfirm={() => quit.mutate()}
      >
        Simulations already on BRAIN keep running there. Queued work waits until you open Alpha
        Harness again.
      </Confirm>
    </aside>
  )
}
