/** Basic Template Research's draft: the open block template, its undo history and the task's settings. */

import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { LAB_DEFAULTS, type LabDraft } from '@/screens/research-labs/lab-task'
import type { TemplateDoc } from '@/screens/research-labs/template-basic/tree'

const UNDO_STEPS = 50

export interface TemplateDraft extends LabDraft {
  /** `preset:<slug>`, a saved template's id, or `null` for a new template. */
  templateId: string | number | null
  name: string
  doc: TemplateDoc | null
  /** Changed since it was opened or saved. */
  dirty: boolean
  /** Earlier trees, newest last. Not kept between visits. */
  past: TemplateDoc[]
}

interface Actions {
  open: (templateId: string | number | null, name: string, doc: TemplateDoc) => void
  edit: (doc: TemplateDoc) => void
  undo: () => void
  saved: (templateId: number, name: string) => void
}

/** What is kept between visits: the open template and the task's settings, not its undo history. */
const persisted = (s: TemplateDraft & Actions) => ({
  region: s.region,
  delay: s.delay,
  universe: s.universe,
  datasetIds: s.datasetIds,
  fieldFilter: s.fieldFilter,
  fields: s.fields,
  rankBy: s.rankBy,
  cores: s.cores,
  simulations: s.simulations,
  decay: s.decay,
  vectorOperators: s.vectorOperators,
  templateId: s.templateId,
  name: s.name,
  doc: s.doc,
  dirty: s.dirty,
})
type Persisted = ReturnType<typeof persisted>

export const useTemplateLab = create<TemplateDraft & Actions>()(
  persist(
    (set, get) => ({
      ...LAB_DEFAULTS,
      templateId: null,
      name: '',
      doc: null,
      dirty: false,
      past: [],
      open: (templateId, name, doc) => set({ templateId, name, doc, dirty: false, past: [] }),
      edit: (doc) => {
        const { doc: current, past } = get()
        set({
          doc,
          dirty: true,
          past: current ? [...past, current].slice(-UNDO_STEPS) : past,
        })
      },
      undo: () => {
        const { past } = get()
        if (past.length === 0) return
        set({ doc: past.at(-1) ?? null, past: past.slice(0, -1), dirty: true })
      },
      saved: (templateId, name) => set({ templateId, name, dirty: false }),
    }),
    {
      name: 'alpha-harness-template-basic-lab',
      // Version 2 leaves cores unchosen, so Settings' default for new tasks applies.
      version: 2,
      migrate: (stored) => ({ ...(stored as Persisted), cores: null }),
      partialize: persisted,
    },
  ),
)
