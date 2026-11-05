/**
 * Live telemetry over one WebSocket: the backend pushes on a single multiplexed channel (see
 * its `realtime.py`) and replays each topic's last message on connect. Reconnects with
 * backoff, because a local backend restarts often.
 */

import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef } from 'react'

export type Topic = 'simulations' | 'tasks' | 'sync' | 'studies' | 'session'

type Handler = (payload: unknown) => void

const RECONNECT_MIN = 500
const RECONNECT_MAX = 10_000
/** Silence that means the socket is gone. The backend pings every 25s when nothing else is
 * happening, so this only fires when the connection died without closing — a sleep, a
 * suspended VM, a dropped tunnel — which leaves the socket reading OPEN forever. */
const SILENCE_MS = 60_000
/** Longer than the backend's 25s ping: an open socket this quiet is presumed dead. */
const PING_MS = 30_000

class Telemetry {
  private socket: WebSocket | null = null
  private handlers = new Map<string, Set<Handler>>()
  private statusHandlers = new Set<(connected: boolean) => void>()
  private delay = RECONNECT_MIN
  private connected = false
  private silence: ReturnType<typeof setTimeout> | null = null
  private lastHeard = 0

  connect(): void {
    if (this.socket && this.socket.readyState <= WebSocket.OPEN) return
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const socket = new WebSocket(`${protocol}//${location.host}/ws`)
    this.socket = socket

    socket.onopen = () => {
      this.delay = RECONNECT_MIN
      this.setConnected(true)
      this.heard()
    }
    socket.onmessage = (event) => {
      this.heard()
      let envelope: { topic?: string; payload?: unknown }
      try {
        envelope = JSON.parse(event.data)
      } catch {
        return
      }
      if (!envelope.topic || envelope.topic === 'ping') return
      for (const handler of this.handlers.get(envelope.topic) ?? []) handler(envelope.payload)
    }
    socket.onclose = () => {
      if (this.silence) clearTimeout(this.silence)
      this.silence = null
      this.setConnected(false)
      this.socket = null
      setTimeout(() => this.connect(), this.delay)
      this.delay = Math.min(this.delay * 2, RECONNECT_MAX)
    }
    socket.onerror = () => socket.close()
  }

  /**
   * Back from a sleep, another window or a dropped network: reconnect now rather than after
   * the backoff, or the silence timer for a socket that died while the machine slept.
   */
  wake(): void {
    this.delay = RECONNECT_MIN
    if (!this.socket) this.connect()
    else if (this.socket.readyState === WebSocket.OPEN && Date.now() - this.lastHeard > PING_MS)
      this.socket.close()
  }

  /** Restart the silence timer. Closing a dead socket is what starts the reconnect. */
  private heard(): void {
    this.lastHeard = Date.now()
    if (this.silence) clearTimeout(this.silence)
    this.silence = setTimeout(() => {
      this.silence = null
      this.socket?.close()
    }, SILENCE_MS)
  }

  private setConnected(connected: boolean): void {
    this.connected = connected
    for (const handler of this.statusHandlers) handler(connected)
  }

  subscribe(topic: Topic, handler: Handler): () => void {
    const set = this.handlers.get(topic) ?? new Set()
    set.add(handler)
    this.handlers.set(topic, set)
    return () => set.delete(handler)
  }

  onStatus(handler: (connected: boolean) => void): () => void {
    this.statusHandlers.add(handler)
    // Report the current state at once: the socket opens long before components mount.
    handler(this.connected)
    return () => this.statusHandlers.delete(handler)
  }
}

export const telemetry = new Telemetry()

window.addEventListener('online', () => telemetry.wake())
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') telemetry.wake()
})

/**
 * Refetch queries under `queryKey` whenever `topic` reports a change. The socket is a signal,
 * not state, so a drifting payload shape can never desync the UI; bursts collapse to one
 * refetch per `minGapMs`, with a trailing call so the final state still lands.
 */
export function useRefetchOn(topic: Topic, queryKey: readonly unknown[], minGapMs = 1000): void {
  const queryClient = useQueryClient()
  const last = useRef(0)
  const pending = useRef<ReturnType<typeof setTimeout> | null>(null)
  // Read when a message lands, so one subscription serves every render.
  const latest = useRef({ queryKey, minGapMs })
  useEffect(() => {
    latest.current = { queryKey, minGapMs }
  })

  useEffect(() => {
    const unsubscribe = telemetry.subscribe(topic, () => {
      const fire = () => {
        last.current = Date.now()
        pending.current = null
        void queryClient.invalidateQueries({ queryKey: latest.current.queryKey })
      }
      const gap = latest.current.minGapMs
      const since = Date.now() - last.current
      if (since >= gap) fire()
      else if (!pending.current) pending.current = setTimeout(fire, gap - since)
    })
    return () => {
      unsubscribe()
      if (pending.current) clearTimeout(pending.current)
      pending.current = null
    }
  }, [topic, queryClient])
}
