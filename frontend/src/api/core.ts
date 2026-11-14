/**
 * Endpoints every screen can lean on: the day's numbers, the session, the simulation
 * engine and background tasks.
 */

import type { components } from './generated'
import { http, qs } from './http'
import {
  type BarStatus,
  type EngineStatus,
  type Scope,
  type Session,
  type SimulationRow,
  scopeQs,
  type TasksSummary,
  type Today,
} from './types'

type Schemas = components['schemas']

export type SettingsField = Omit<Schemas['SettingsField'], 'choices'> & {
  choices: { value: string | number; label: string }[] | null
}

export type SettingsOptions = Omit<Schemas['SettingsOptions'], 'fields'> & {
  fields: Record<string, SettingsField>
}

/** What the Settings screen saves. */
export type Preferences = Schemas['Preferences']

export const preferences = {
  get: () => http.get<Preferences>('/api/preferences'),
  /** Replaces every choice; the engine applies it on its next round. */
  put: (body: Preferences) => http.put<Preferences>('/api/preferences', body),
}

export type Competition = Schemas['Competition']

export const competitions = {
  /** Ongoing first, soonest to end. One BRAIN read, plus one per ongoing competition. */
  list: () => http.get<Schemas['Competitions']>('/api/competitions'),
}

export const today = {
  /** The first screen in one call. Scope defaults to USA / D1 / TOP3000. */
  get: (scope?: Partial<Scope>) => http.get<Today>(`/api/today${scopeQs(scope)}`),
  /** The header clocks. Cheap. */
  bar: () => http.get<BarStatus>('/api/today/bar'),
  /** The Dashboard's one click: the tasks it started, and why any of the run fell short. */
  run: (simulations: number) => http.post<Schemas['TodayRun']>('/api/today/run', { simulations }),
}

/** What a person sends when they ask for help. */
export const diagnostics = {
  /** A scrubbed zip of the version, the system and the end of the log, saved in Downloads. */
  export: () => http.post<Schemas['Exported']>('/api/diagnostics/export'),
  openLogs: () => http.post<void>('/api/diagnostics/open-logs'),
}

export const auth = {
  /** Omit both to sign in with the stored credential. */
  login: (email?: string, password?: string) =>
    http.post<Session>('/api/auth/login', {
      email: email || null,
      password: password || null,
    }),
  /** Close a paused sign-in's identity check. Answering early is normal: the session comes
   *  back unauthenticated and still carrying the inquiry. */
  verify: (inquiry: string) => http.post<Session>('/api/auth/verify', { inquiry }),
  newCheck: () => http.post<Session>('/api/auth/new-check'),
  logout: () => http.post<Session>('/api/auth/logout'),
  /** Legal values for every settings field, given what is chosen. Keys use BRAIN's names. */
  settingsOptions: (settings: Record<string, unknown>) =>
    http.post<SettingsOptions>('/api/auth/settings-options', { settings }),
}

export const simulations = {
  /** Every PENDING and RUNNING record, oldest first. Queued work is counted by `engine()`. */
  active: () => http.get<SimulationRow[]>('/api/simulations/active'),
  engine: () => http.get<EngineStatus>('/api/simulations/engine'),
  /** Cancel by record id. Cancel the batch PARENT: a child cannot be cancelled on BRAIN. */
  cancel: (recordId: number, force = false) =>
    http.post<{ acknowledged: boolean; simulation: SimulationRow | null }>(
      `/api/simulations/${recordId}/cancel${force ? '?force=true' : ''}`,
    ),
  /** Drop queued work that has not been sent. Omit `task` to drop everything queued. */
  dropQueue: (task?: string) =>
    http.del<{ dropped: number }>(`/api/simulations/queue${qs({ task })}`),
}

export const tasks = {
  list: () => http.get<TasksSummary>('/api/tasks'),
}

export type UpdateStatus = Schemas['UpdateStatus']
export type UpdateStarted = Schemas['UpdateStarted']

export const update = {
  /** GitHub's last answer, asked only as often as Settings allow. `refresh` asks now, at
   *  most once a minute however often it is pressed. */
  status: (refresh = false) =>
    http.get<UpdateStatus>(`/api/update${qs({ refresh: refresh || null })}`),
  /** Hands the install to the launcher and closes the app so it can run. */
  apply: () => http.post<UpdateStarted>('/api/update'),
  /** Closes the app for good: the launcher exits with it rather than starting it again. */
  quit: () => http.post<Schemas['Quitting']>('/api/quit'),
}
