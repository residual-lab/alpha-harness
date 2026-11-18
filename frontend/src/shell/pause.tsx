/**
 * Why nothing is being sent to BRAIN right now. The header chip, the Matrix, the notifications
 * and the window title all say it in the same words.
 */

import { useQuery } from '@tanstack/react-query'
import { useEffect, useRef } from 'react'
import { simulations } from '@/api/core'
import type { EngineStatus } from '@/api/types'
import { inDesktop, notify } from './desktop'

export interface Pause {
  /** `identity` is finished in the banner above every screen, so the Matrix leaves it out. */
  key: 'identity' | 'offline' | 'session' | 'limit' | 'busy'
  chip: string
  title: string
  body: string
}

const QUEUED = 'Queued work stays queued.'

/** Most pressing first; empty while sending runs. */
export function pausesOf(status: EngineStatus | undefined): Pause[] {
  if (!status) return []
  const pauses: Pause[] = []
  const state = status.connection.state
  if (state === 'VERIFYING') {
    pauses.push({
      key: 'identity',
      chip: 'Identity check',
      title: 'BRAIN needs to verify your identity',
      body: `Sending is paused until BRAIN accepts the check. ${QUEUED}`,
    })
  } else if (state === 'VERIFYING_EXPIRED') {
    pauses.push({
      key: 'identity',
      chip: 'Identity check',
      title: "BRAIN's identity check expired",
      body: `It closed before it was finished: start a new check to resume sending. ${QUEUED}`,
    })
  } else if (state === 'OFFLINE') {
    pauses.push({
      key: 'offline',
      chip: 'Offline',
      title: 'BRAIN cannot be reached',
      body: `This computer has no connection to BRAIN. Sending and reading results resume by themselves once it is back. ${QUEUED}`,
    })
  } else if (state === 'SIGNED_OUT' || status.sessionLost) {
    pauses.push({
      key: 'session',
      chip: 'Session lost',
      title: 'BRAIN is not accepting your session',
      body: `Sending is paused. It resumes by itself once the session is renewed, or as soon as you sign in again. ${QUEUED}`,
    })
  }
  if (status.dailyLimitHit) {
    pauses.push({
      key: 'limit',
      chip: 'Daily limit',
      title: "BRAIN's daily simulation limit is reached",
      body: `Nothing more will be sent until the allowance resets at midnight US Eastern. ${QUEUED}`,
    })
  }
  if (status.busyElsewhere) {
    pauses.push({
      key: 'busy',
      chip: 'Busy elsewhere',
      title: 'BRAIN is busy with simulations started elsewhere',
      body: 'BRAIN is already running simulations you started elsewhere (website or notebook). Alpha Harness waits for free slots.',
    })
  }
  return pauses
}

/** Tells someone who is not looking: a notification when sending stops, and the window title
 * for as long as it stays stopped. */
export function PauseAlerts() {
  const engine = useQuery({ queryKey: ['simulations', 'engine'], queryFn: simulations.engine })
  const pauses = pausesOf(engine.data)

  const seen = useRef<Set<string> | null>(null)
  useEffect(() => {
    if (!engine.data) return
    const now = pausesOf(engine.data)
    const before = seen.current
    seen.current = new Set(now.map((pause) => pause.title))
    // Only what changes while the app is open: what was so when it opened is on screen.
    if (before)
      for (const pause of now) if (!before.has(pause.title)) void notify(pause.title, pause.body)
  }, [engine.data])

  const paused = pauses.length > 0
  useEffect(() => {
    document.title = paused ? 'Paused · Alpha Harness' : 'Alpha Harness'
  }, [paused])

  useEffect(() => {
    if (inDesktop || !('Notification' in window) || Notification.permission !== 'default') return
    // Browsers ask only from a click, so the workspace's first one asks.
    const ask = () => void Notification.requestPermission()
    window.addEventListener('pointerdown', ask, { once: true })
    return () => window.removeEventListener('pointerdown', ask)
  }, [])

  return null
}
