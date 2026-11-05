/** The Template Lab draft: the template being written, its variables and the task's settings. */

import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import {
  DEFAULT_DECAY,
  DEFAULT_NAN_HANDLING,
  DEFAULT_PASTEURIZATION,
  DEFAULT_TRUNCATION,
  TEST_PERIOD_YEARS,
} from '@/api/platform'
import { DEFAULT_SCOPE } from '@/lib/scope'
import { followOtherTabs } from '@/lib/tabs'
import type { Investability, VariableDef } from './api'

export interface TemplateDraft {
  region: string
  delay: number
  /** The template as typed, `$variables` and all. */
  text: string
  /** Definitions given here, by name without the `$`. A name without one starts from its
   *  preset, or from the dataset, subcategory or category it is named after. */
  variables: Record<string, VariableDef>
  /** Searched, like the variables. Nothing is ticked until the user ticks it. */
  universes: string[]
  neutralizations: string[]
  investability: Investability[]
  /** As typed, so a number half-way through being typed is not snapped back. */
  decay: string
  truncation: string
  pasteurization: 'ON' | 'OFF'
  nanHandling: 'ON' | 'OFF'
  testYears: string
  testMonths: string
  /** `null` until chosen in the form: until then Settings' default applies. */
  cores: number | null
  /** `null` until the user assigns them: a task always has simulations chosen on purpose. */
  simulations: number | null
  /** The saved template open, or `null` for a new one. */
  templateId: number | null
  name: string
  /** Changed since it was opened or saved. */
  dirty: boolean
  /** The template as it was opened or last saved: what Reset puts back. */
  opened: { text: string; variables: Record<string, VariableDef> }
  /** The variable whose fields are being chosen in the Data Explorer, kept across a reload. */
  picking: string | null
  /** The same template, typed or built from blocks. */
  view: 'code' | 'blocks'
}

interface Actions {
  open: (
    templateId: number | null,
    name: string,
    text: string,
    variables: Record<string, VariableDef>,
  ) => void
  write: (text: string) => void
  /** `null` drops the definition, so the name starts from its default again. */
  define: (name: string, variable: VariableDef | null) => void
  saved: (templateId: number, name: string) => void
}

const DRAFT: TemplateDraft = {
  region: DEFAULT_SCOPE.region,
  delay: DEFAULT_SCOPE.delay,
  text: '',
  variables: {},
  universes: [],
  neutralizations: [],
  investability: [],
  decay: String(DEFAULT_DECAY),
  truncation: String(DEFAULT_TRUNCATION),
  pasteurization: DEFAULT_PASTEURIZATION,
  nanHandling: DEFAULT_NAN_HANDLING,
  testYears: String(TEST_PERIOD_YEARS),
  testMonths: '0',
  cores: null,
  simulations: null,
  templateId: null,
  name: '',
  dirty: false,
  opened: { text: '', variables: {} },
  picking: null,
  view: 'code',
}

/** Every variable with its defaults filled in. */
function whole(variables: Record<string, VariableDef> | undefined): Record<string, VariableDef> {
  return Object.fromEntries(
    Object.entries(variables ?? {}).map(([name, v]) => [
      name,
      v.kind === 'fields'
        ? { ...v, dataset_ids: v.dataset_ids ?? [] }
        : { ...v, values: v.values ?? '' },
    ]),
  )
}

export const useTemplateLab = create<TemplateDraft & Actions>()(
  persist(
    (set) => ({
      ...DRAFT,
      open: (templateId, name, text, variables) =>
        set({ templateId, name, text, variables, dirty: false, opened: { text, variables } }),
      write: (text) => set({ text, dirty: true }),
      define: (name, variable) =>
        set((s) => {
          const variables = { ...s.variables }
          if (variable) variables[name] = variable
          else delete variables[name]
          return { variables, dirty: true }
        }),
      saved: (templateId, name) =>
        set((s) => ({
          templateId,
          name,
          dirty: false,
          opened: { text: s.text, variables: s.variables },
        })),
    }),
    {
      name: 'alpha-harness-template-lab',
      // Version 3 types templates; a draft of blocks keeps only its market and its size.
      // Version 4 repairs a version 3 draft holding a variable opened from a saved template
      // without its defaults: a fields variable with no `dataset_ids` failed the whole screen.
      version: 4,
      migrate: (stored, version) => {
        const old = (stored ?? {}) as Partial<TemplateDraft>
        if (version === 3)
          return {
            ...DRAFT,
            ...old,
            variables: whole(old.variables),
            opened: { text: old.opened?.text ?? '', variables: whole(old.opened?.variables) },
          }
        return {
          ...DRAFT,
          region: old.region ?? DRAFT.region,
          delay: old.delay ?? DRAFT.delay,
          cores: old.cores ?? null,
          simulations: old.simulations ?? null,
        }
      },
      partialize: ({ open: _open, write: _write, define: _define, saved: _saved, ...draft }) =>
        draft,
    },
  ),
)
followOtherTabs(useTemplateLab)
