/**
 * The gate and the workspace. `GET /api/today` decides: backend down, sign-in, or work.
 */

import { useQuery } from '@tanstack/react-query'
import { Outlet } from '@tanstack/react-router'
import { RefreshCwIcon, ServerCrashIcon, XIcon } from 'lucide-react'
import { Suspense, useEffect, useState } from 'react'
import { Group, Panel, useDefaultLayout } from 'react-resizable-panels'
import { today } from '@/api/core'
import { errorMessage } from '@/api/http'
import type { Today } from '@/api/types'
import { useLive } from '@/lib/live'
import { useRefetchOn } from '@/lib/ws'
import { Button, Empty, LINK, Notice, Skeleton, Spinner } from '@/ui/kit'
import { ResizeHandle, useMediaQuery, WIDE } from '@/ui/panels'
import { CommandMenu } from './command-menu'
import { DesktopNotifications, inDesktop } from './desktop'
import { Header } from './header'
import { SettingsDialog } from './settings'
import { SIDEBAR_WIDTH, Sidebar, useSidebarHidden } from './sidebar'
import { SignIn } from './sign-in'
import { InstallingOverlay } from './update'

export function Shell() {
  const query = useQuery({
    queryKey: ['today'],
    queryFn: () => today.get(),
    // While it has never answered, keep asking: a backend restarting after an update, or put
    // back after a failed one, is down for seconds, and this window should come back by itself.
    refetchInterval: (q) =>
      q.state.data === undefined && q.state.status === 'error' ? 3000 : false,
  })
  useRefetchOn('session', ['today'])

  if (query.isPending) {
    return (
      <div className="flex h-svh items-center justify-center">
        <Spinner className="size-5" />
      </div>
    )
  }

  // Only a load that never succeeded: a failed background refetch keeps its data, and swapping
  // the whole workspace for this screen would lose open dialogs and unsaved forms.
  if (query.data === undefined) {
    return (
      <div className="flex h-svh items-center justify-center p-6">
        <div className="w-full max-w-md rounded-lg border border-hairline bg-surface-1">
          {import.meta.env.DEV ? (
            <Empty icon={<ServerCrashIcon />} title="The backend is not answering">
              <p>{errorMessage(query.error)}</p>
              <code className="mt-3 block rounded-md bg-canvas px-2 py-1.5 text-caption break-all text-ink-muted">
                cd backend && uv run uvicorn alpha_harness.main:app --reload --port 8000
              </code>
            </Empty>
          ) : (
            <Empty icon={<ServerCrashIcon />} title="Alpha Harness is not running">
              <p>
                This window reconnects by itself as soon as it is back. If it does not come back,
                open Alpha Harness again.
              </p>
              <p className="mt-2">
                If it keeps stopping, the reason is in <span className="num">launcher.log</span> in
                its folder: on Windows,{' '}
                <span className="num break-all">%LOCALAPPDATA%\AlphaHarness</span>.
              </p>
            </Empty>
          )}
          <div className="flex justify-center border-t border-hairline p-3">
            <Button onClick={() => query.refetch()} loading={query.isFetching}>
              <RefreshCwIcon /> Try again
            </Button>
          </div>
        </div>
      </div>
    )
  }

  if (query.data.step === 'sign-in') return <SignIn storedEmail={query.data.you.email} />
  return <Workspace you={query.data.you} />
}

/** The top bar over sidebar | workspace, draggable from `lg` up. Dragged below its minimum the
 * sidebar collapses to the icon rail; below `lg` the rail is all there is; Ctrl+B (⌘B) hides it
 * altogether, and hovering its button then peeks it out over the page. The bar spans both
 * columns so that button never moves, and the peek is the docked sidebar drawn in the same
 * place, so the three states differ only in what the page beside it does. One keyed group at
 * every width, so crossing 1024px or hiding the sidebar does not remount the screen and throw
 * away whatever was half-typed in it. */
function Workspace({ you }: { you: Today['you'] }) {
  const wide = useMediaQuery(WIDE)
  const [dragCollapsed, setDragCollapsed] = useState(false)
  const hidden = useSidebarHidden((s) => s.hidden)
  const peek = useSidebarHidden((s) => s.peek)
  const openPeek = useSidebarHidden((s) => s.openPeek)
  const closePeek = useSidebarHidden((s) => s.closePeek)
  const width = useSidebarHidden((s) => s.width)
  const setWidth = useSidebarHidden((s) => s.setWidth)
  const { defaultLayout, onLayoutChanged } = useDefaultLayout({
    id: 'ah-panels:shell',
    storage: localStorage,
    onlySaveAfterUserInteractions: true,
    // A layout per panel set, so a hidden sidebar comes back at the width it was dragged to.
    panelIds: hidden ? ['workspace'] : ['sidebar', 'workspace'],
  })
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && !event.altKey && event.key.toLowerCase() === 'b') {
        event.preventDefault()
        useSidebarHidden.getState().toggle()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
  const rail = !wide || dragCollapsed
  return (
    <div className="flex h-svh flex-col">
      <Header />
      {inDesktop && <DesktopNotifications />}
      <Group
        id="shell"
        orientation="horizontal"
        defaultLayout={defaultLayout}
        onLayoutChanged={onLayoutChanged}
        className="min-h-0 flex-1"
      >
        {!hidden && (
          <Panel
            key="sidebar"
            id="sidebar"
            defaultSize={wide ? SIDEBAR_WIDTH : 52}
            minSize={wide ? 200 : 52}
            maxSize={wide ? 340 : 52}
            collapsible={wide}
            collapsedSize={52}
            onResize={(size) => {
              setDragCollapsed(size.inPixels <= 64)
              if (size.inPixels > 64) setWidth(size.inPixels)
            }}
          >
            <Sidebar you={you} collapsed={rail} />
          </Panel>
        )}
        {wide && !hidden && <ResizeHandle key="handle" variant="edge" />}
        <Panel key="workspace" id="workspace" minSize={wide ? 480 : 0} className="min-w-0">
          <div className="flex h-full min-h-0 min-w-0 flex-col">
            <VerificationBanner />
            <main className="min-h-0 flex-1 overflow-auto">
              <Suspense fallback={<ScreenSkeleton />}>
                <Outlet />
              </Suspense>
            </main>
          </div>
        </Panel>
      </Group>
      {hidden && (
        <>
          {/* The left edge opens it too, so the pointer need not find the button. */}
          <div
            aria-hidden
            className="fixed top-12 bottom-0 left-0 z-30 w-2"
            onMouseEnter={openPeek}
            onMouseLeave={closePeek}
          />
          {/* The docked sidebar exactly, over the page rather than beside it: same top, same
              width, rail or not, so only the page stays where it was. */}
          {peek && (
            <div
              className="fixed top-12 bottom-0 left-0 z-40 shadow-float"
              style={{ width: rail ? 52 : width }}
            >
              <Sidebar
                you={you}
                collapsed={rail}
                onMouseEnter={openPeek}
                onMouseLeave={closePeek}
              />
            </div>
          )}
        </>
      )}
      <CommandMenu />
      <SettingsDialog />
      <InstallingOverlay />
    </div>
  )
}

function VerificationBanner() {
  const url = useLive((s) => s.verificationUrl)
  if (!url) return null
  return (
    <div className="border-b border-hairline p-2">
      <Notice
        tone="warn"
        title="BRAIN needs to verify your identity"
        action={
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="Dismiss"
            onClick={() => useLive.setState({ verificationUrl: null })}
          >
            <XIcon />
          </Button>
        }
      >
        Finish the check in your browser, then sign in again.{' '}
        <a href={url} target="_blank" rel="noopener noreferrer" className={LINK}>
          Open the verification page
        </a>
      </Notice>
    </div>
  )
}

function ScreenSkeleton() {
  return (
    <div className="flex flex-col gap-3 p-4">
      <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
        {Array.from({ length: 4 }, (_, i) => (
          <Skeleton key={i} className="h-24" />
        ))}
      </div>
      <Skeleton className="h-80" label="Loading" />
    </div>
  )
}
