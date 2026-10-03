/**
 * Basic Template Research, Template Lab's classic block builder: open a template, change it
 * with blocks, choose datasets and settings, then add its search to Tasks. The lab tries each
 * choice and variable value the template allows and keeps what gives the best Sharpe.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CopyPlusIcon, EllipsisIcon, FilePlusIcon, SaveIcon, StarIcon } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { toast } from 'sonner'
import { cn } from '@/lib/cn'
import { fmt } from '@/lib/format'
import { useCores } from '@/lib/preferences'
import { AddTaskButtons, useAddTask } from '@/screens/research-labs/add-task'
import {
  labBody,
  MAX_SIMULATIONS,
  simulationsValid,
  useLabMarket,
  useLabPreview,
  vectorOperatorsOf,
} from '@/screens/research-labs/lab-task'
import { DatasetsPanel, SettingsPanel } from '@/screens/research-labs/task-settings'
import {
  type TemplateLabRequest,
  type TemplateSummary,
  templateLab,
} from '@/screens/research-labs/template-basic/api'
import type { Blocks, TemplateDoc } from '@/screens/research-labs/template-basic/tree'
import { TemplateSections } from '@/screens/research-labs/template-sections'
import {
  Button,
  Empty,
  ErrorNotice,
  Field,
  Input,
  Notice,
  Page,
  PageHeader,
  Panel,
  Skeleton,
} from '@/ui/kit'
import { Confirm, Dialog, Menu } from '@/ui/overlay'
import { Builder } from './builder'
import { useTemplateLab } from './state'

const EMPTY: TemplateDoc = { version: 1, root: null }

interface Openable {
  id: string | number | null
  name: string
  tree: TemplateDoc
}

interface Naming {
  name: string
  description: string | null
}

/** Operators are synced from BRAIN once a session, so blocks and presets match the account. */
let syncedThisSession = false

export function BasicTemplateScreen() {
  const draft = useTemplateLab()
  const set = useTemplateLab.setState
  const queryClient = useQueryClient()
  const { chosen, panel } = useLabMarket(draft, set, '/labs/template/basic')
  const [naming, setNaming] = useState<'save-as' | 'rename' | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [opening, setOpening] = useState<Openable | null>(null)

  const options = useQuery({
    queryKey: ['template-basic-lab', 'options'],
    queryFn: async () => {
      const data = await templateLab.options(!syncedThisSession)
      syncedThisSession = true
      return data
    },
    staleTime: 5 * 60_000,
  })
  const blocks: Blocks = useMemo(
    () => Object.fromEntries((options.data?.blocks ?? []).map((block) => [block.name, block])),
    [options.data],
  )
  const templates = useQuery({
    queryKey: ['template-basic-lab', 'templates'],
    queryFn: templateLab.templates,
    enabled: options.isSuccess,
  })
  const list = useMemo(() => templates.data?.templates ?? [], [templates.data])
  const current = list.find((t) => t.id === draft.templateId)
  const refreshTemplates = () =>
    queryClient.invalidateQueries({ queryKey: ['template-basic-lab', 'templates'] })

  // The first visit opens the first preset, so the canvas never starts blank.
  const firstTemplate = list[0]
  useEffect(() => {
    if (firstTemplate && useTemplateLab.getState().doc === null) {
      useTemplateLab.getState().open(firstTemplate.id, firstTemplate.name, firstTemplate.tree)
    }
  }, [firstTemplate])

  const doc = draft.doc ?? EMPTY
  const vectorOperators = vectorOperatorsOf(draft, options.data?.vector)
  const cores = useCores(draft.cores)
  const previewBody: TemplateLabRequest = {
    ...labBody(draft, vectorOperators, cores),
    tree: doc,
    template_name: '',
  }
  const { preview, current: planned } = useLabPreview(
    'template-basic-lab',
    previewBody,
    templateLab.preview,
    { enabled: options.isSuccess, wait: 400 },
  )
  const plan = preview.data
  const templateProblems = plan?.templateProblems ?? []
  const marketPlan =
    plan && chosen
      ? {
          ...plan,
          problems: plan.problems.filter((problem) => !templateProblems.includes(problem)),
        }
      : undefined

  const maxSimulations = options.data?.maxSimulations ?? MAX_SIMULATIONS
  const saved = typeof draft.templateId === 'number' ? draft.templateId : null
  const taskName = !draft.name
    ? 'New Template'
    : draft.dirty && current?.preset
      ? `${draft.name} (Edited)`
      : draft.name
  const ready =
    plan !== undefined &&
    chosen &&
    planned &&
    plan.problems.length === 0 &&
    simulationsValid(draft.simulations, maxSimulations)

  const add = useAddTask(() =>
    templateLab.addTask({
      ...previewBody,
      template_name: taskName,
      simulations: draft.simulations ?? 0,
    }),
  )
  const sync = useMutation({
    mutationFn: () => templateLab.options(true),
    onSuccess: (data) => {
      queryClient.setQueryData(['template-basic-lab', 'options'], data)
      void refreshTemplates()
      toast.success(`${fmt.int(data.operators.count)} operators synced`)
    },
  })
  const save = useMutation({
    mutationFn: (id: number) =>
      templateLab.update(id, {
        name: draft.name,
        description: current?.description ?? null,
        tree: doc,
      }),
    onSuccess: (row) => {
      draft.saved(Number(row.id), row.name)
      void refreshTemplates()
      toast.success('Template saved')
    },
  })
  const saveAs = useMutation({
    meta: { inline: true },
    mutationFn: (value: Naming) => templateLab.create({ ...value, tree: doc }),
    onSuccess: (row) => {
      draft.saved(Number(row.id), row.name)
      setNaming(null)
      void refreshTemplates()
      toast.success('Template saved')
    },
  })
  const rename = useMutation({
    meta: { inline: true },
    // Renaming keeps the saved blocks; unsaved changes stay unsaved.
    mutationFn: (value: Naming) =>
      templateLab.update(saved ?? 0, { ...value, tree: current?.tree ?? doc }),
    onSuccess: (row) => {
      useTemplateLab.setState({ name: row.name })
      setNaming(null)
      void refreshTemplates()
      toast.success('Template renamed')
    },
  })
  const remove = useMutation({
    mutationFn: (id: number) => templateLab.remove(id),
    onSuccess: () => {
      setDeleting(false)
      const preset = list.find((t) => t.preset)
      if (preset) draft.open(preset.id, preset.name, preset.tree)
      else draft.open(null, '', EMPTY)
      void refreshTemplates()
      toast.success('Template deleted')
    },
  })

  const openTemplate = (next: Openable) => {
    if (useTemplateLab.getState().dirty) setOpening(next)
    else draft.open(next.id, next.name, next.tree)
  }

  return (
    <Page>
      <PageHeader
        title="Template Lab"
        actions={
          <>
            {saved !== null && (
              <Button
                disabled={!draft.dirty}
                loading={save.isPending}
                onClick={() => save.mutate(saved)}
              >
                <SaveIcon />
                Save
              </Button>
            )}
            <Button onClick={() => setNaming('save-as')}>
              <CopyPlusIcon />
              Save As
            </Button>
            {saved !== null && (
              <Menu
                trigger={
                  <Button size="icon" variant="ghost" aria-label="More template actions">
                    <EllipsisIcon />
                  </Button>
                }
                items={[
                  { label: 'Rename', onClick: () => setNaming('rename') },
                  {
                    label: 'Delete',
                    danger: true,
                    onClick: () => setDeleting(true),
                  },
                ]}
              />
            )}
            <AddTaskButtons add={add} disabled={!ready} />
          </>
        }
      />
      <TemplateSections />
      {options.isError && (
        <ErrorNotice error={options.error} title="Could not read your operators" />
      )}
      {options.data && !options.data.operators.synced && (
        <Notice
          tone="error"
          title="Your BRAIN operators could not be read. Sign in again, then Sync Operators."
        />
      )}

      <Panel
        title="Templates"
        actions={
          <Button
            size="sm"
            variant="ghost"
            onClick={() => openTemplate({ id: null, name: '', tree: EMPTY })}
          >
            <FilePlusIcon />
            New Template
          </Button>
        }
      >
        {templates.isError ? (
          <ErrorNotice error={templates.error} title="Could not load templates" />
        ) : templates.isPending ? (
          // Templates wait on the operators; their failure is already reported above.
          !options.isError && <Skeleton className="h-40" />
        ) : list.length === 0 ? (
          <Empty title="No templates yet">Start one with New Template.</Empty>
        ) : (
          <div className="flex flex-col gap-4">
            <TemplateGrid
              label="Presets"
              items={list.filter((t) => t.preset)}
              selected={draft.templateId}
              dirty={draft.dirty}
              onOpen={openTemplate}
            />
            <TemplateGrid
              label="Saved"
              items={list.filter((t) => !t.preset)}
              selected={draft.templateId}
              dirty={draft.dirty}
              onOpen={openTemplate}
            />
          </div>
        )}
      </Panel>

      <Builder
        doc={doc}
        blocks={blocks}
        options={options.data}
        problems={templateProblems}
        skeleton={plan?.skeleton}
        canUndo={draft.past.length > 0}
        syncing={sync.isPending}
        onChange={draft.edit}
        onUndo={draft.undo}
        onSync={() => sync.mutate()}
      />

      <DatasetsPanel {...panel} />
      <SettingsPanel
        draft={draft}
        set={set}
        vector={options.data?.vector ?? []}
        chosenVector={vectorOperators}
        decays={options.data?.decays}
        maxSimulations={maxSimulations}
        plan={marketPlan}
        error={chosen && preview.isError ? preview.error : null}
      />

      {naming && (
        <NameDialog
          title={naming === 'rename' ? 'Rename Template' : 'Save As'}
          initialName={naming === 'rename' ? draft.name : draft.name ? `${draft.name} Copy` : ''}
          initialDescription={current?.description ?? ''}
          pending={saveAs.isPending || rename.isPending}
          error={naming === 'rename' ? rename.error : saveAs.error}
          onClose={() => {
            setNaming(null)
            saveAs.reset()
            rename.reset()
          }}
          onSubmit={(value) => (naming === 'rename' ? rename.mutate(value) : saveAs.mutate(value))}
        />
      )}
      <Confirm
        open={opening !== null}
        onOpenChange={(open) => !open && setOpening(null)}
        title="Discard unsaved changes?"
        confirmLabel="Discard"
        danger
        onConfirm={() => {
          if (opening) draft.open(opening.id, opening.name, opening.tree)
          setOpening(null)
        }}
      />
      <Confirm
        open={deleting}
        onOpenChange={setDeleting}
        title={`Delete ${draft.name}?`}
        confirmLabel="Delete"
        danger
        pending={remove.isPending}
        onConfirm={() => saved !== null && remove.mutate(saved)}
      >
        Tasks already added keep their own copy.
      </Confirm>
    </Page>
  )
}

function TemplateGrid({
  label,
  items,
  selected,
  dirty,
  onOpen,
}: {
  label: string
  items: TemplateSummary[]
  selected: string | number | null
  dirty: boolean
  onOpen: (template: Openable) => void
}) {
  if (items.length === 0) return null
  return (
    <div className="flex flex-col gap-2">
      <h3 className="text-caption font-medium text-ink-subtle">{label}</h3>
      <div className="-m-1 grid max-h-64 gap-2 overflow-y-auto p-1 sm:grid-cols-2 xl:grid-cols-5">
        {items.map((template) => {
          const open = template.id === selected
          return (
            <button
              key={String(template.id)}
              type="button"
              aria-pressed={open}
              onClick={() => onOpen(template)}
              className={cn(
                'flex min-h-20 flex-col gap-1 rounded-md border px-3 py-2 text-left transition-colors',
                open
                  ? 'border-hairline-strong bg-surface-2'
                  : 'border-hairline bg-surface-1 hover:border-hairline-strong hover:bg-surface-2',
              )}
            >
              <span className="flex items-center justify-between gap-2">
                <span className="flex min-w-0 items-center gap-1.5">
                  <span className="text-title min-w-0 truncate" title={template.name}>
                    {template.name}
                  </span>
                  {template.source && (
                    <StarIcon
                      className="size-3 shrink-0 fill-current text-status-warning"
                      aria-label="Starred"
                    />
                  )}
                </span>
                {open && dirty && (
                  <span className="shrink-0 text-body-compact text-ink-subtle">Edited</span>
                )}
              </span>
              {template.description && (
                <span className="line-clamp-2 text-body-compact text-ink-subtle">
                  {template.description}
                </span>
              )}
              {template.source && (
                <span className="text-body-compact break-words text-ink-subtle">
                  Source: {template.source}
                </span>
              )}
              {template.missing.length > 0 && (
                <span className="text-body-compact break-words text-status-warning">
                  Needs {template.missing.join(', ')}
                </span>
              )}
            </button>
          )
        })}
      </div>
    </div>
  )
}

function NameDialog({
  title,
  initialName,
  initialDescription,
  pending,
  error,
  onClose,
  onSubmit,
}: {
  title: string
  initialName: string
  initialDescription: string
  pending: boolean
  error: unknown
  onClose: () => void
  onSubmit: (value: Naming) => void
}) {
  const [name, setName] = useState(initialName)
  const [description, setDescription] = useState(initialDescription)
  const submit = () =>
    name.trim() && onSubmit({ name: name.trim(), description: description.trim() || null })

  return (
    <Dialog
      open
      onOpenChange={(open) => !open && onClose()}
      title={title}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" disabled={!name.trim()} loading={pending} onClick={submit}>
            Save
          </Button>
        </>
      }
    >
      <form
        className="flex flex-col gap-3"
        onSubmit={(event) => {
          event.preventDefault()
          submit()
        }}
      >
        <Field label="Name">
          <Input autoFocus maxLength={128} value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="Description">
          <Input
            maxLength={500}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </Field>
        {error ? <ErrorNotice error={error} title="Could not save the template" /> : null}
      </form>
    </Dialog>
  )
}
