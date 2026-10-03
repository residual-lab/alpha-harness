/** Tasks: what the labs added. Only the Tasks tab runs them. */

import type { components } from '@/api/generated'
import { http, qs } from '@/api/http'

type Schemas = components['schemas']

/** IDLE: not started. QUEUED: run, waiting for its cores to fit in the free slots. */
export type TaskStatus = Schemas['StudyStatus']
export type LabTask = Schemas['LabTask']
export type LabTasks = Schemas['LabTasks']
export type RankedAlpha = Omit<Schemas['RankedAlpha'], 'settings'> & {
  settings: { universe?: string; neutralization?: string; [key: string]: unknown } | null
}

/** A submittable Alpha from any task, with the task that found it. */
export type TaskAlpha = Omit<Schemas['TaskAlpha'], 'settings'> & {
  settings: RankedAlpha['settings']
}

export type TaskInfo = Schemas['TaskInfo']
export type AlphaGroups = Schemas['AlphaGroups']
export type AlphaGroup = Schemas['AlphaGroup']
export type GroupAlpha = Schemas['GroupAlpha']

export interface CloneRequest {
  simulations?: number | null
  /** LLM Power Pool Lab only: send another prompt; `prompt_id` null sends the built-in. */
  change_prompt?: boolean
  prompt_id?: number | null
  run?: boolean
}

export type PowerPoolCorrelation = Schemas['PowerPoolCorrelation']
export type PowerPoolRow = Schemas['PowerPoolRow']

const B = '/api/lab-tasks'

export const labTasks = {
  list: () => http.get<LabTasks>(B),
  /** Spends simulation quota. */
  runAll: () => http.post<LabTasks>(`${B}/run-all`),
  /** Spends simulation quota. Also resumes a paused task. */
  run: (id: number) => http.post<LabTask>(`${B}/${id}/run`),
  pause: (id: number) => http.post<LabTask>(`${B}/${id}/pause`),
  /** Carries on from where it left off; `simulations` more are added to its target. */
  continue: (id: number, simulations = 0) =>
    http.post<LabTask>(`${B}/${id}/continue`, { simulations }),
  /** A fresh copy: same setup, nothing it did. */
  clone: (id: number, body: CloneRequest) =>
    http.post<Schemas['AddedTask']>(`${B}/${id}/clone`, body),
  info: (id: number) => http.get<TaskInfo>(`${B}/${id}/info`),
  /** Region Agnostic Lab: each Alpha with its run in every region, most submittable first. */
  groups: (id: number) => http.get<AlphaGroups>(`${B}/${id}/groups`),
  /** Runs BRAIN's submission checks on the task's Alphas, in the background. No quota. */
  calibrate: (id: number) => http.post<Schemas['WorkflowStarted']>(`${B}/${id}/calibrate`),
  stop: (id: number) => http.post<LabTask>(`${B}/${id}/stop`),
  /** A blank name clears it. */
  rename: (id: number, name: string) => http.put<LabTask>(`${B}/${id}/name`, { name }),
  change: (id: number, body: { cores?: number; simulations?: number }) =>
    http.patch<LabTask>(`${B}/${id}`, body),
  /** `force` takes a running task down too: paused, what is out cancelled, then removed. */
  remove: (id: number, force = false) =>
    http.del<Schemas['TaskRemoved']>(`${B}/${id}${qs({ force: force || undefined })}`),
  top: (id: number, limit = 50) => http.get<RankedAlpha[]>(`${B}/${id}/top${qs({ limit })}`),
  /** Every Alpha from every task that nothing refuses: each check PASS, WARNING or PENDING. */
  submittable: () => http.get<TaskAlpha[]>(`${B}/submittable`),
  /** Measured locally against the submitted Power Pool, from the PnL already stored. */
  powerPoolFor: (alphaIds: string[]) =>
    http.post<PowerPoolCorrelation>(`${B}/power-pool-correlation`, { alphaIds }),
  /** Downloads PnL, then turnover for the Alphas that satisfy Power Pool Correlation. */
  powerPoolWorkflow: (alphaIds: string[]) =>
    http.post<Schemas['WorkflowStarted']>(`${B}/power-pool-workflow`, { alphaIds }),
  /** Asks BRAIN, in this order, for each Production Correlation it has not given yet. */
  prodCorrelation: (alphaIds: string[]) =>
    http.post<Schemas['WorkflowStarted']>(`${B}/prod-correlation`, { alphaIds }),
  /** Stops that check; the answers already back stay. */
  stopProdCorrelation: () => http.post<void>(`${B}/prod-correlation/stop`),
}
