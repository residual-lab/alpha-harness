/**
 * Template Lab: type a Fast Expression with a `$name` wherever the search chooses, define each
 * variable as fields or values, pick the settings to search, then add the search to Tasks. The
 * lab tries what the template allows and keeps what gives the best Sharpe.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from '@tanstack/react-router'
import { CopyPlusIcon, EllipsisIcon, RotateCcwIcon, SaveIcon } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { toast } from 'sonner'
import { catalog } from '@/api/catalog'
import type { Scope } from '@/api/types'
import { DASH, fmt } from '@/lib/format'
import { useCores } from '@/lib/preferences'
import { isRegionAgnostic, regionLabel, useScope, useScopeOptions } from '@/lib/scope'
import { useDatasetPick } from '@/screens/data/dataset-pick'
import { useFieldFilter } from '@/screens/data/state'
import { AddTaskButtons, useAddTask } from '@/screens/research-labs/add-task'
import { MAX_SIMULATIONS, simulationsValid, useLabPreview } from '@/screens/research-labs/lab-task'
import { NeutralizationPicker } from '@/screens/research-labs/neutralization'
import {
  clamp,
  SimulationSettingsFields,
  testPeriodOf,
} from '@/screens/research-labs/simulation-settings'
import { CoresSetting, SimulationsSetting } from '@/screens/research-labs/task-settings'
import { TemplateSections } from '@/screens/research-labs/template-sections'
import {
  Button,
  Chips,
  Disclosure,
  ErrorNotice,
  Field,
  Fieldset,
  Input,
  Metric,
  Notice,
  Page,
  PageHeader,
  Panel,
  Segmented,
  Skeleton,
} from '@/ui/kit'
import { Confirm, Dialog, Menu } from '@/ui/overlay'
import { ScopePicker } from '@/ui/scope-picker'
import {
  type FieldsVariable,
  INVESTABILITY_LABELS,
  type Investability,
  type Sizing,
  type TemplateLabRequest,
  templateLab,
  type VariableDef,
} from './api'
import { TemplateBlocks } from './blocks'
import { TemplateEditor } from './editor'
import { Facts, GALLERY, galleryKey, savedKey, TemplatesPanel } from './gallery'
import { useTemplateLab } from './state'
import { defaultOf, namesIn, resolve, VariablesPanel } from './variables'

interface Naming {
  name: string
  description: string | null
}

/** What opening a template writes into the draft: a saved one, a built-in one, or nothing. */
interface Openable {
  id: number | null
  name: string
  text: string
  variables: Record<string, VariableDef>
}

const BLANK: Openable = { id: null, name: '', text: '', variables: {} }

/** Operators are synced from BRAIN once a session, so checks match the account. */
let syncedThisSession = false

/**
 * The market as one of its universes, for the catalog's own queries: a market's universes hold
 * the same fields, bar a few, so the one with the most stands for all. `null` when it is not
 * downloaded, `undefined` until that is known.
 */
function useMarketScope(region: string, delay: number): Scope | null | undefined {
  const scopes = useQuery({ queryKey: ['catalog', 'scopes'], queryFn: catalog.scopes })
  return useMemo(() => {
    if (!scopes.data) return undefined
    const rows = scopes.data.filter(
      (r) => r.instrument_type === 'EQUITY' && r.region === region && r.delay === delay,
    )
    const widest = rows.reduce<(typeof rows)[number] | null>(
      (best, r) => (best && best.fields >= r.fields ? best : r),
      null,
    )
    return widest ? { instrumentType: 'EQUITY', region, delay, universe: widest.universe } : null
  }, [scopes.data, region, delay])
}

export function TemplateLabScreen() {
  const draft = useTemplateLab()
  const set = useTemplateLab.setState
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [, setDataScope] = useScope('data')
  const [naming, setNaming] = useState<'save-as' | 'rename' | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [opening, setOpening] = useState<Openable | null>(null)

  const options = useQuery({
    queryKey: ['template-lab', 'options'],
    queryFn: async () => {
      const data = await templateLab.options(!syncedThisSession)
      syncedThisSession = true
      return data
    },
    staleTime: 5 * 60_000,
  })
  const templates = useQuery({
    queryKey: ['template-lab', 'templates'],
    queryFn: templateLab.templates,
  })
  const current = templates.data?.templates.find((t) => t.id === draft.templateId)
  const refreshTemplates = () =>
    queryClient.invalidateQueries({ queryKey: ['template-lab', 'templates'] })

  const known = useMarketScope(draft.region, draft.delay)
  const scope = known ?? null
  const datasets = useQuery({
    queryKey: ['catalog', 'datasets', scope, ''],
    queryFn: () => (scope ? catalog.datasets(scope) : Promise.resolve([])),
    enabled: scope !== null,
  })
  const presets = useMemo(() => options.data?.presets ?? {}, [options.data])

  // Every variable the template writes, and what each stands for: given here, or its default.
  const names = useMemo(() => namesIn(draft.text), [draft.text])
  const defs = useMemo(
    () =>
      Object.fromEntries(
        names.map((n) => [n, draft.variables[n] ?? defaultOf(n, presets, datasets.data ?? [])]),
      ) as Record<string, VariableDef | null>,
    [names, draft.variables, presets, datasets.data],
  )
  const given = useMemo(
    () => new Set(names.filter((n) => draft.variables[n])),
    [names, draft.variables],
  )
  // Every card's template, sized in one request: built-ins by default, saved ones as saved.
  const list = templates.data?.templates
  const cards = useMemo(() => {
    const found: { key: string; sizing: Sizing }[] = []
    const add = (key: string, text: string, own: Record<string, VariableDef>) => {
      found.push({
        key,
        sizing: { text, variables: resolve(text, own, presets, datasets.data ?? []) },
      })
    }
    for (const [f, family] of GALLERY.entries()) {
      add(galleryKey(f, null), family.text, {})
      for (const [v, text] of family.variations.entries()) add(galleryKey(f, v), text, {})
    }
    for (const t of list ?? []) add(savedKey(t.id), t.text, t.variables)
    return found
  }, [presets, datasets.data, list])
  const sized = useQuery({
    queryKey: ['template-lab', 'stats', draft.region, draft.delay, cards.map((c) => c.sizing)],
    queryFn: () =>
      templateLab.stats({
        region: draft.region,
        delay: draft.delay,
        templates: cards.map((c) => c.sizing),
      }),
    enabled: options.isSuccess && known !== undefined && (scope === null || datasets.isFetched),
  })
  const cardInfo = useMemo(
    () =>
      new Map(
        cards.map(
          (c, i) =>
            [c.key, { variables: c.sizing.variables, stats: sized.data?.stats[i] }] as const,
        ),
      ),
    [cards, sized.data],
  )

  // Back from the Data Explorer with one variable's datasets and filter.
  useEffect(() => {
    const pick = useDatasetPick.getState().take('/labs/template')
    const { picking, variables } = useTemplateLab.getState()
    if (!pick || !picking) {
      // A pick cancelled in the Explorer leaves nothing to take.
      if (picking) useTemplateLab.setState({ picking: null })
      return
    }
    const before = variables[picking]
    useTemplateLab.setState({
      region: pick.scope.region,
      delay: pick.scope.delay,
      picking: null,
      dirty: true,
      variables: {
        ...variables,
        [picking]: {
          kind: 'fields',
          dataset_ids: pick.ids,
          filter: pick.extra?.filter ?? null,
          vector_operators: before?.kind === 'fields' ? (before.vector_operators ?? []) : [],
        },
      },
    })
  }, [])

  const moreFilters = (name: string, variable: FieldsVariable) => {
    if (!scope) return
    set({ picking: name, variables: { ...draft.variables, [name]: variable }, dirty: true })
    // The Explorer opens on this variable's own filter, so it shows the fields it will use.
    useFieldFilter.getState().replace({ ...(variable.filter ?? {}) })
    useDatasetPick.getState().start(scope, variable.dataset_ids, '/labs/template')
    setDataScope(scope)
    void navigate({ to: '/data' })
  }

  const cores = useCores(draft.cores)
  const truncation = Number(draft.truncation)
  const body: TemplateLabRequest = {
    region: draft.region,
    delay: draft.delay,
    template: draft.text,
    variables: Object.fromEntries(names.flatMap((n) => (defs[n] ? [[n, defs[n]] as const] : []))),
    universes: draft.universes,
    neutralizations: draft.neutralizations,
    investability: draft.investability,
    decay: clamp(draft.decay, 512),
    truncation: Number.isFinite(truncation) ? Math.min(1, Math.max(0, truncation)) : 0.08,
    pasteurization: draft.pasteurization,
    nan_handling: draft.nanHandling,
    test_period: testPeriodOf(draft.testYears, draft.testMonths),
    cores,
    template_name: '',
  }
  // Each plan carries the text it was made for, so its problems are never drawn on newer text.
  const previewed = (sent: TemplateLabRequest) =>
    templateLab.preview(sent).then((plan) => ({ ...plan, text: sent.template }))
  const { preview, current: planned } = useLabPreview('template-lab', body, previewed, {
    // Only once every default can be read, so a name never shows as undefined in passing.
    enabled: options.isSuccess && known !== undefined && (scope === null || datasets.isFetched),
    wait: 400,
  })
  const plan = preview.data
  const infos = useMemo(() => new Map(plan?.variables.map((v) => [v.name, v])), [plan])
  const reference = useMemo(() => options.data?.reference ?? [], [options.data])
  // The problems that name something in the text, drawn there by the editor.
  const placed = useMemo(
    () =>
      plan?.text === draft.text
        ? [
            ...plan.templateProblems,
            ...plan.variables.flatMap((v) => v.problems.map((p) => `$${v.name}: ${p}`)),
          ]
        : [],
    [plan, draft.text],
  )

  const maxSimulations = options.data?.maxSimulations ?? MAX_SIMULATIONS
  const saved = draft.templateId
  const ready =
    plan !== undefined &&
    planned &&
    plan.problems.length === 0 &&
    simulationsValid(draft.simulations, maxSimulations)
  const toSave = () => ({
    text: draft.text,
    variables: Object.fromEntries(
      names.flatMap((n) => {
        const own = draft.variables[n]
        return own ? [[n, own] as const] : []
      }),
    ),
  })

  const add = useAddTask(() =>
    templateLab.addTask({
      ...body,
      template_name: draft.name || 'New Template',
      simulations: draft.simulations ?? 0,
    }),
  )
  const sync = useMutation({
    mutationFn: () => templateLab.options(true),
    onSuccess: (data) => {
      queryClient.setQueryData(['template-lab', 'options'], data)
      void refreshTemplates()
      toast.success(`${fmt.int(data.operators.count)} operators synced`)
    },
  })
  const save = useMutation({
    mutationFn: (id: number) =>
      templateLab.update(id, {
        name: draft.name,
        description: current?.description ?? null,
        ...toSave(),
      }),
    onSuccess: (row) => {
      draft.saved(row.id, row.name)
      void refreshTemplates()
      toast.success('Template saved')
    },
  })
  const saveAs = useMutation({
    meta: { inline: true },
    mutationFn: (value: Naming) => templateLab.create({ ...value, ...toSave() }),
    onSuccess: (row) => {
      draft.saved(row.id, row.name)
      setNaming(null)
      void refreshTemplates()
      toast.success('Template saved')
    },
  })
  const rename = useMutation({
    meta: { inline: true },
    // Renaming keeps what was saved; unsaved changes stay unsaved.
    mutationFn: (value: Naming) =>
      templateLab.update(saved ?? 0, {
        ...value,
        text: current?.text ?? draft.text,
        variables: current?.variables ?? {},
      }),
    onSuccess: (row) => {
      set({ name: row.name })
      setNaming(null)
      void refreshTemplates()
      toast.success('Template renamed')
    },
  })
  const remove = useMutation({
    mutationFn: (id: number) => templateLab.remove(id),
    onSuccess: () => {
      setDeleting(false)
      set({ templateId: null, name: '', dirty: true })
      void refreshTemplates()
      toast.success('Template deleted')
    },
  })

  const openTemplate = (next: Openable) => {
    if (draft.dirty) setOpening(next)
    else apply(next)
  }
  const apply = (next: Openable) => draft.open(next.id, next.name, next.text, next.variables)

  const { neutralizations } = useScopeOptions({
    instrumentType: 'EQUITY',
    region: draft.region,
    delay: draft.delay,
    universe: '',
  })

  return (
    <Page>
      <PageHeader title="Template Lab" actions={<AddTaskButtons add={add} disabled={!ready} />} />
      <TemplateSections />
      {options.isError && (
        <ErrorNotice error={options.error} title="Could not read your operators" />
      )}
      {options.data && !options.data.operators.synced && (
        <Notice
          tone="error"
          title="Your BRAIN operators could not be read. Sign in again, then Sync Operators."
          action={
            <Button size="sm" loading={sync.isPending} onClick={() => sync.mutate()}>
              Sync Operators
            </Button>
          }
        />
      )}

      <Panel title="Market" description="The Region and Delay every Alpha in the task runs in.">
        <div className="flex flex-col gap-3">
          <ScopePicker
            parts={['region', 'delay']}
            scope={{
              instrumentType: 'EQUITY',
              region: draft.region,
              delay: draft.delay,
              universe: '',
            }}
            onChange={(change) => {
              const next: { region?: string; delay?: number } = {}
              if (change.region !== undefined) next.region = change.region
              if (change.delay !== undefined) next.delay = change.delay
              if (Object.keys(next).length) set(next)
            }}
          />
          {scope === null && (
            <Notice
              tone="warn"
              title={`${regionLabel(draft.region)} delay ${draft.delay} is not downloaded yet`}
            >
              Download it in BRAIN › Sync to choose fields from it.
            </Notice>
          )}
          {isRegionAgnostic(draft) && (
            <Notice tone="info" title="Every alpha here runs in four regions at once">
              One simulation covers USA, Europe, Asia and Global, and spends four of today's
              allowance. The alphas it makes can be submitted where two or more of those regions
              hold up.
            </Notice>
          )}
        </div>
      </Panel>

      <TemplatesPanel
        saved={list ?? []}
        savedState={{
          pending: templates.isPending,
          error: templates.isError ? templates.error : null,
        }}
        selected={draft.templateId}
        dirty={draft.dirty}
        cards={cardInfo}
        onOpenText={(text) => openTemplate({ ...BLANK, text })}
        onOpenSaved={(t) =>
          openTemplate({ id: t.id, name: t.name, text: t.text, variables: t.variables })
        }
        onNew={() => openTemplate(BLANK)}
      />

      <Panel
        title={draft.name || 'New Template'}
        actions={
          <>
            <Button
              size="sm"
              variant="ghost"
              disabled={!draft.dirty}
              title="Back to how it was when opened or last saved"
              onClick={() =>
                openTemplate({ id: draft.templateId, name: draft.name, ...draft.opened })
              }
            >
              <RotateCcwIcon />
              Reset
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setNaming('save-as')}>
              <CopyPlusIcon />
              Save As
            </Button>
            {saved !== null && (
              <>
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={!draft.dirty}
                  loading={save.isPending}
                  onClick={() => save.mutate(saved)}
                >
                  <SaveIcon />
                  Save
                </Button>
                <Menu
                  trigger={
                    <Button size="icon-sm" variant="ghost" aria-label="More template actions">
                      <EllipsisIcon />
                    </Button>
                  }
                  items={[
                    { label: 'Rename', onClick: () => setNaming('rename') },
                    { label: 'Delete', danger: true, onClick: () => setDeleting(true) },
                  ]}
                />
              </>
            )}
          </>
        }
        description={
          <>
            A Fast Expression with <code className="num">$name</code> wherever the search chooses.
            Lines <code className="num">name = …;</code> name its steps and the last line is the
            Alpha; <code className="num">$name(…)</code> chooses an operator.
          </>
        }
      >
        <div className="flex flex-col gap-3">
          <Segmented
            label="Edit the template as"
            items={[
              { value: 'code', label: 'Code' },
              { value: 'blocks', label: 'Blocks' },
            ]}
            value={draft.view}
            onChange={(view) => set({ view })}
          />
          {draft.view === 'blocks' ? (
            <TemplateBlocks
              text={draft.text}
              onChange={draft.write}
              reference={reference}
              variables={[
                ...new Set([...names, ...Object.keys(presets), ...Object.keys(draft.variables)]),
              ]}
              scope={scope}
              onCode={() => set({ view: 'code' })}
            />
          ) : (
            <TemplateEditor
              value={draft.text}
              onChange={draft.write}
              reference={reference}
              presets={presets}
              variables={defs}
              infos={infos}
              problems={placed}
              scope={scope}
            />
          )}
          {draft.text.trim() && <Facts stats={plan?.stats} />}
          {plan?.text === draft.text &&
            plan.templateProblems.map((problem) => (
              <Notice key={problem} tone="error" title={problem} />
            ))}
        </div>
      </Panel>

      <VariablesPanel
        names={names}
        defs={defs}
        given={given}
        infos={infos}
        scope={scope}
        presets={presets}
        vector={options.data?.vector ?? []}
        pending={!planned}
        onDefine={draft.define}
        onMoreFilters={moreFilters}
      />

      <Panel
        title="Simulation Settings"
        description="Universe, Neutralization and Investability are searched: tick one or several. The rest hold one value for every simulation."
      >
        <div className="flex flex-col gap-4">
          <Fieldset legend="Universe">
            {plan ? (
              plan.universes.length ? (
                <Chips
                  label="Universe"
                  items={plan.universes.map((u) => ({ value: u, label: u }))}
                  value={draft.universes.filter((u) => plan.universes.includes(u))}
                  onChange={(next) =>
                    set({
                      universes: [
                        ...draft.universes.filter((u) => !plan.universes.includes(u)),
                        ...next,
                      ],
                    })
                  }
                />
              ) : (
                <span className="text-body-compact text-ink-subtle">{DASH}</span>
              )
            ) : (
              <Skeleton className="h-8 w-64" />
            )}
          </Fieldset>
          {neutralizations.length > 0 && (
            <NeutralizationPicker
              available={neutralizations}
              value={draft.neutralizations}
              onChange={(next) => set({ neutralizations: next })}
            />
          )}
          <Fieldset
            legend="Investability"
            hint="Max Trade and Max Position cap positions by liquidity; BRAIN takes one at a time."
          >
            {plan ? (
              <Chips
                label="Investability"
                items={(plan.investability as Investability[]).map((v) => ({
                  value: v,
                  label: INVESTABILITY_LABELS[v],
                }))}
                value={draft.investability.filter((v) => plan.investability.includes(v))}
                onChange={(next) =>
                  set({
                    investability: [
                      ...draft.investability.filter((v) => !plan.investability.includes(v)),
                      ...next,
                    ],
                  })
                }
              />
            ) : (
              <Skeleton className="h-8 w-64" />
            )}
          </Fieldset>
          <SimulationSettingsFields
            decay={draft.decay}
            setDecay={(decay) => set({ decay })}
            truncation={draft.truncation}
            setTruncation={(next) => set({ truncation: next })}
            pasteurization={draft.pasteurization}
            setPasteurization={(pasteurization) => set({ pasteurization })}
            nanHandling={draft.nanHandling}
            setNanHandling={(nanHandling) => set({ nanHandling })}
            testYears={draft.testYears}
            setTestYears={(testYears) => set({ testYears })}
            testMonths={draft.testMonths}
            setTestMonths={(testMonths) => set({ testMonths })}
          />
          {plan?.settingsProblems.map((problem) => (
            <Notice key={problem} tone="error" title={problem} />
          ))}
        </div>
      </Panel>

      <Panel title="Cores & Simulations">
        <div className="flex flex-col gap-4">
          <div className="flex flex-wrap items-start gap-x-8 gap-y-4">
            <CoresSetting value={cores} onChange={(next) => set({ cores: next })} />
            <SimulationsSetting
              value={draft.simulations}
              max={maxSimulations}
              onChange={(next) => set({ simulations: next })}
            />
          </div>
          <div className="grid gap-3 sm:grid-cols-3">
            <Metric boxed label="Market" value={`${regionLabel(draft.region)} · D${draft.delay}`} />
            <Metric
              boxed
              label="Search Space"
              value={plan?.combinations ? fmt.int(plan.combinations) : DASH}
              hint="Different simulations the template and settings allow"
            />
            <Metric boxed label="Variables" value={fmt.int(names.length)} />
          </div>
          {draft.simulations !== null && draft.simulations > maxSimulations && (
            <Notice
              tone="error"
              title={`A task takes at most ${fmt.int(maxSimulations)} simulations.`}
            />
          )}
          {preview.isError && <ErrorNotice error={preview.error} title="Could not plan the task" />}
          {plan?.warnings.map((m) => (
            <Notice key={m} tone="warn" title={m} />
          ))}
          {plan && plan.sample.length > 0 && (
            <Disclosure summary="Sample Alphas">
              <ul className="flex flex-col gap-2">
                {plan.sample.map((s, i) => (
                  <li
                    key={i}
                    className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5"
                  >
                    <code className="num text-body-compact break-all text-ink">{s.expression}</code>
                    <span className="text-body-compact text-ink-subtle">
                      {[
                        s.settings['universe'],
                        s.settings['neutralization'],
                        s.settings['maxTrade'] === 'ON'
                          ? 'Max Trade'
                          : s.settings['maxPosition'] === 'ON'
                            ? 'Max Position'
                            : null,
                      ]
                        .filter(Boolean)
                        .map(String)
                        .join(' · ')}
                    </span>
                  </li>
                ))}
              </ul>
            </Disclosure>
          )}
        </div>
      </Panel>

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
          if (opening) apply(opening)
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
