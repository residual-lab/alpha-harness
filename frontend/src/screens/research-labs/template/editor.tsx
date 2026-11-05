/**
 * The template editor: CodeMirror, with Fast Expression coloured the way VS Code's Dark+ theme
 * colours code, bracket pairs coloured by depth, completions for operators, `$variables`,
 * grouping and data fields, hovers that document whatever is under the pointer, and the
 * preview's problems drawn on the text they are about.
 */

import {
  acceptCompletion,
  autocompletion,
  type Completion,
  type CompletionContext,
  type CompletionResult,
  closeBrackets,
  closeBracketsKeymap,
  completionKeymap,
  snippetCompletion,
} from '@codemirror/autocomplete'
import { defaultKeymap, history, historyKeymap } from '@codemirror/commands'
import {
  bracketMatching,
  HighlightStyle,
  indentOnInput,
  syntaxHighlighting,
} from '@codemirror/language'
import { type Diagnostic, forceLinting, linter, lintKeymap } from '@codemirror/lint'
import { highlightSelectionMatches, searchKeymap } from '@codemirror/search'
import {
  Annotation,
  EditorState,
  RangeSetBuilder,
  StateEffect,
  StateField,
} from '@codemirror/state'
import {
  Decoration,
  type DecorationSet,
  drawSelection,
  dropCursor,
  EditorView,
  highlightActiveLine,
  highlightActiveLineGutter,
  highlightSpecialChars,
  hoverTooltip,
  keymap,
  lineNumbers,
  rectangularSelection,
  showTooltip,
  type Tooltip,
  ViewPlugin,
  type ViewUpdate,
} from '@codemirror/view'
import { tags as t } from '@lezer/highlight'
import { useEffect, useRef } from 'react'
import { catalog, type DataFieldDetail } from '@/api/catalog'
import { fmt } from '@/lib/format'
import type { VariableDef, VariableInfo } from './api'
import {
  brackets,
  code,
  fastExpression,
  GROUPS,
  group,
  hole,
  inputsOf,
  local,
  locals,
  NAMES,
  type OperatorDoc,
  snippetOf,
  variable,
  WORDS,
} from './fast-expression'
import { namesIn } from './variables'

/** What the editor reads at the moment it is asked: kept in a ref, so nothing rebuilds it. */
interface Live {
  reference: OperatorDoc[]
  presets: Record<string, string>
  variables: Record<string, VariableDef | null>
  infos: Map<string, VariableInfo>
  problems: string[]
  onChange: (text: string) => void
}

/** One colour per kind of name; the rest is VS Code's Dark+ and its bracket pair colours. */
const COLOUR = {
  operator: '#dcdcaa',
  matrix: '#9cdcfe',
  vector: '#4ec9b0',
  group: '#ffab70',
  variable: '#c586c0',
}

const darkPlus = HighlightStyle.define([
  { tag: t.function(t.variableName), color: COLOUR.operator },
  { tag: t.variableName, color: COLOUR.matrix },
  { tag: t.propertyName, color: '#9d9d9d', fontStyle: 'italic' },
  { tag: local, color: COLOUR.variable },
  { tag: group, color: COLOUR.group },
  { tag: variable, color: COLOUR.variable, fontWeight: '600' },
  {
    tag: hole,
    color: '#f48771',
    backgroundColor: 'rgb(244 135 113 / 0.14)',
    outline: '1px dashed rgb(244 135 113 / 0.6)',
    borderRadius: '3px',
  },
  { tag: t.number, color: '#b5cea8' },
  { tag: t.string, color: '#ce9178' },
  { tag: t.comment, color: '#6a9955', fontStyle: 'italic' },
  { tag: t.bool, color: '#569cd6' },
  { tag: t.operator, color: '#d4d4d4' },
  { tag: t.punctuation, color: '#d4d4d4' },
  { tag: t.invalid, color: '#f44747' },
  { tag: brackets[0], color: '#ffd700' },
  { tag: brackets[1], color: '#da70d6' },
  { tag: brackets[2], color: '#179fff' },
])

const theme = EditorView.theme(
  {
    '&': {
      color: '#d4d4d4',
      backgroundColor: '#1e1e1e',
      fontSize: '13px',
    },
    '&.cm-focused': { outline: 'none' },
    '.cm-scroller': { fontFamily: 'var(--font-mono)', lineHeight: '1.65' },
    '.cm-content': { caretColor: '#aeafad', padding: '10px 0', minHeight: '9rem' },
    '.cm-cursor, .cm-dropCursor': { borderLeftColor: '#aeafad', borderLeftWidth: '2px' },
    '.cm-gutters': { backgroundColor: '#1e1e1e', color: '#6e7681', border: 'none' },
    '.cm-lineNumbers .cm-gutterElement': { padding: '0 12px 0 16px', minWidth: '40px' },
    '.cm-activeLineGutter': { backgroundColor: 'transparent', color: '#cccccc' },
    '.cm-activeLine': { backgroundColor: 'rgb(255 255 255 / 0.04)' },
    '&.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground, .cm-selectionBackground, .cm-content ::selection':
      { backgroundColor: '#264f78' },
    '.cm-selectionMatch': { backgroundColor: 'rgb(173 214 255 / 0.15)' },
    '&.cm-focused .cm-matchingBracket': {
      backgroundColor: 'rgb(0 100 0 / 0.1)',
      outline: '1px solid #888888',
    },
    '&.cm-focused .cm-nonmatchingBracket': { color: '#f44747' },
    '.cm-fx-local, .cm-fx-local *': { color: COLOUR.variable },
    '.cm-fx-vector, .cm-fx-vector *': { color: COLOUR.vector },
    '.cm-fx-group, .cm-fx-group *': { color: COLOUR.group },
    '.cm-tooltip': {
      backgroundColor: '#252526',
      color: '#cccccc',
      border: '1px solid #454545',
      borderRadius: '4px',
      boxShadow: '0 4px 16px rgb(0 0 0 / 0.45)',
    },
    '.cm-tooltip-autocomplete > ul': { fontFamily: 'var(--font-mono)', maxHeight: '18rem' },
    '.cm-tooltip-autocomplete > ul > li': { padding: '2px 8px' },
    '.cm-tooltip-autocomplete > ul > li[aria-selected]': {
      backgroundColor: '#04395e',
      color: '#ffffff',
    },
    '.cm-completionMatchedText': { color: '#2aaaff', textDecoration: 'none', fontWeight: '600' },
    '.cm-completionDetail': { color: '#9d9d9d', fontStyle: 'normal', marginLeft: '1.5em' },
    '.cm-completionIcon-function': { color: '#b180d7' },
    '.cm-completionIcon-variable': { color: '#75beff' },
    '.cm-completionIcon-constant': { color: '#4ec9b0' },
    '.cm-completionIcon-property': { color: '#9cdcfe' },
    '.cm-completionInfo': { padding: '8px 10px', maxWidth: '28rem' },
    '.cm-fx-doc': {
      display: 'flex',
      flexDirection: 'column',
      gap: '6px',
      padding: '8px 10px',
      maxWidth: '30rem',
    },
    '.cm-fx-doc code': { fontFamily: 'var(--font-mono)', color: '#dcdcaa', whiteSpace: 'pre-wrap' },
    '.cm-fx-doc .cm-fx-kind': { color: '#9d9d9d', fontSize: '12px' },
    // Only while typing here: left behind, it would sit over the editor.
    '&:not(.cm-focused) .cm-fx-signature': { display: 'none' },
    '.cm-fx-doc code strong': { color: '#ffffff', fontWeight: '700', textDecoration: 'underline' },
    '.cm-fx-doc p': { margin: '0', lineHeight: '1.5' },
    '.cm-diagnostic': { padding: '6px 10px', borderLeftWidth: '3px' },
    '.cm-diagnostic-error': { borderLeftColor: '#f14c4c' },
    '.cm-diagnostic-warning': { borderLeftColor: '#cca700' },
  },
  { dark: true },
)

// --- reading the document -------------------------------------------------------------

/** Redraws the names once a field's type arrives. */
const recolour = StateEffect.define<null>()

/**
 * Colours a local's uses like its definition, and a field by its catalog type, which the text
 * alone cannot tell: a VECTOR or GROUP field reads differently from a MATRIX one.
 */
function nameColours() {
  return ViewPlugin.fromClass(
    class {
      decorations: DecorationSet
      gone = false
      readonly view: EditorView
      constructor(view: EditorView) {
        this.view = view
        this.decorations = this.marked()
      }
      update(update: ViewUpdate) {
        if (
          update.docChanged ||
          update.transactions.some((tr) => tr.effects.some((e) => e.is(recolour)))
        )
          this.decorations = this.marked()
      }
      destroy() {
        this.gone = true
      }
      marked(): DecorationSet {
        const blank = code(this.view.state.doc.toString())
        const names = locals(blank)
        const builder = new RangeSetBuilder<Decoration>()
        for (const m of blank.matchAll(NAMES)) {
          const name = m[0]
          let mark: Decoration | undefined
          if (names.has(name)) mark = MARK.local
          else if (!GROUPS.has(name) && !WORDS.includes(name.toLowerCase()))
            mark = this.fieldMark(name)
          if (mark) builder.add(m.index, m.index + name.length, mark)
        }
        return builder.finish()
      }
      fieldMark(name: string): Decoration | undefined {
        if (!typed.has(name)) {
          typed.set(name, null)
          void lookUp(name).then((field) => {
            typed.set(name, field?.field_type ?? null)
            if (field && !this.gone) this.view.dispatch({ effects: recolour.of(null) })
          })
        }
        const type = typed.get(name)
        return type === 'VECTOR' ? MARK.vector : type === 'GROUP' ? MARK.group : undefined
      }
    },
    { decorations: (plugin) => plugin.decorations },
  )
}

const MARK = {
  local: Decoration.mark({ class: 'cm-fx-local' }),
  vector: Decoration.mark({ class: 'cm-fx-vector' }),
  group: Decoration.mark({ class: 'cm-fx-group' }),
}

/** A field's type by name, once its lookup has answered. */
const typed = new Map<string, string | null>()

/** The word under `pos`, `$` included. */
function wordAt(view: EditorView, pos: number): { from: number; to: number; text: string } | null {
  const line = view.state.doc.lineAt(pos)
  for (const m of line.text.matchAll(/\$?[A-Za-z_]\w*|\.\.\./g)) {
    const from = line.from + m.index
    const to = from + m[0].length
    if (from <= pos && pos <= to) return { from, to, text: m[0] }
  }
  return null
}

// --- what the editor tells the reader ---------------------------------------------------

function element(tag: string, text: string, className?: string): HTMLElement {
  const node = document.createElement(tag)
  node.textContent = text
  if (className) node.className = className
  return node
}

function doc(parts: (HTMLElement | null)[]): HTMLElement {
  const box = document.createElement('div')
  box.className = 'cm-fx-doc'
  for (const part of parts) if (part) box.append(part)
  return box
}

function operatorDoc(op: OperatorDoc): HTMLElement {
  return doc([
    element('code', op.definition.split(/\r?\n/)[0] ?? op.name),
    op.category ? element('span', op.category, 'cm-fx-kind') : null,
    op.description ? element('p', op.description) : null,
  ])
}

/** The call the caret is inside, and which of its inputs it is on, read back from the caret
 *  over the code alone: a `;` or the start of the text ends the search. */
function callAt(text: string, pos: number): { name: string; from: number; input: number } | null {
  const src = code(text)
  let depth = 0
  let input = 0
  for (let i = pos - 1; i >= 0; i--) {
    const ch = src[i]
    if (ch === ')') depth += 1
    else if (ch === '(') {
      if (depth > 0) {
        depth -= 1
        continue
      }
      const name = /([A-Za-z_]\w*)\s*$/.exec(src.slice(Math.max(0, i - 80), i))
      return name?.[1] ? { name: name[1], from: i - name[0].length, input } : null
    } else if (ch === ',' && depth === 0) input += 1
    else if (ch === ';' && depth === 0) return null
  }
  return null
}

/** `ts_rank(x, d)` with the input being typed in bold, so a lookback is never put in x's place. */
function signature(op: OperatorDoc, input: number): HTMLElement {
  const line = document.createElement('code')
  const inputs = inputsOf(op)
  line.append(`${op.name}(`)
  inputs.forEach((name, i) => {
    if (i > 0) line.append(', ')
    line.append(element(i === input ? 'strong' : 'span', name))
  })
  line.append(')')
  const box = doc([line])
  box.classList.add('cm-fx-signature')
  return box
}

/** The signature of the call the caret is in, above it while typing. */
const signatures = (live: { current: Live }) =>
  StateField.define<Tooltip | null>({
    create: () => null,
    update(_, tr) {
      const { main } = tr.state.selection
      if (!main.empty) return null
      const call = callAt(tr.state.doc.toString(), main.head)
      const op = call && live.current.reference.find((o) => o.name === call.name)
      if (!call || !op) return null
      return {
        pos: call.from,
        above: true,
        strictSide: true,
        create: () => ({ dom: signature(op, call.input) }),
      }
    },
    provide: (field) => showTooltip.from(field),
  })

function describe(name: string, live: Live): string {
  const def = live.variables[name]
  const info = live.infos.get(name)
  if (!def) return 'Not defined yet: choose its fields or type its values in Variables.'
  if (def.kind === 'values') {
    const count = info?.kind === 'values' ? ` (${fmt.int(info.values)})` : ''
    return `Values${count}: ${def.values}`
  }
  const ids = def.dataset_ids
  const from =
    ids.length > 3 ? `${ids.slice(0, 3).join(', ')} and ${ids.length - 3} more` : ids.join(', ')
  const count = info?.fields ? `, ${fmt.int(info.fields.total)} fields` : ''
  return `Fields from ${from || 'no dataset yet'}${count}`
}

/** Fields looked up by hovering, by name: each is asked about once. A field is the same
 *  field in every market, so the editor needs none. */
const looked = new Map<string, Promise<DataFieldDetail | null>>()

function lookUp(name: string): Promise<DataFieldDetail | null> {
  let found = looked.get(name)
  if (!found) {
    found = catalog.fieldAnywhere(name).catch(() => {
      // A miss is not remembered, so a name that was briefly unreachable, or that a sync has
      // since brought, is asked again.
      looked.delete(name)
      return null
    })
    looked.set(name, found)
  }
  return found
}

async function hover(view: EditorView, pos: number, live: Live): Promise<Tooltip | null> {
  const word = wordAt(view, pos)
  // A word inside a comment or a string is prose, not code.
  if (!word || !code(view.state.doc.toString()).slice(word.from, word.to).trim()) return null
  const tip = (dom: HTMLElement): Tooltip => ({
    pos: word.from,
    end: word.to,
    above: true,
    create: () => ({ dom }),
  })
  const after = view.state.doc.sliceString(word.to, word.to + 40)
  if (word.text === '...') {
    return tip(doc([element('p', 'A signal goes here. Write it before the template can run.')]))
  }
  if (word.text.startsWith('$')) {
    const name = word.text.slice(1)
    const preset = live.presets[name]
    return tip(
      doc([
        element('code', word.text),
        element('span', 'Variable: the search chooses it for each Alpha', 'cm-fx-kind'),
        element('p', describe(name, live)),
        preset !== undefined ? element('p', `Preset: ${preset}`, 'cm-fx-kind') : null,
      ]),
    )
  }
  if (/^\s*\(/.test(after)) {
    const op = live.reference.find((o) => o.name === word.text)
    return tip(
      op ? operatorDoc(op) : doc([element('p', `${word.text} is not one of your operators.`)]),
    )
  }
  if (GROUPS.has(word.text)) {
    return tip(
      doc([
        element('code', word.text),
        element(
          'p',
          'Grouping field: groups stocks for group operators, and is never counted as data.',
        ),
      ]),
    )
  }
  if (locals(code(view.state.doc.toString())).has(word.text)) {
    return tip(
      doc([element('code', word.text), element('p', 'Defined by a line of this template.')]),
    )
  }
  if (WORDS.includes(word.text.toLowerCase())) return null
  const field = await lookUp(word.text)
  if (!field) return null
  return tip(
    doc([
      element('code', field.field_id),
      element(
        'span',
        [field.field_type, field.dataset_id, field.category_name].filter(Boolean).join(' · '),
        'cm-fx-kind',
      ),
      field.description ? element('p', field.description) : null,
    ]),
  )
}

async function complete(context: CompletionContext, live: Live): Promise<CompletionResult | null> {
  // A bare `$` already asks for the variables, as VS Code suggests on a trigger character.
  const word = context.matchBefore(/\$?[A-Za-z_]\w*|\$/)
  if (!word && !context.explicit) return null
  const from = word?.from ?? context.pos
  const typed = word?.text ?? ''
  const text = context.state.doc.toString()

  if (typed.startsWith('$')) {
    const names = new Set([
      ...Object.keys(live.presets),
      ...namesIn(text),
      ...Object.keys(live.variables),
    ])
    return {
      from,
      options: [...names].map((name): Completion => {
        const detail = live.presets[name] ?? (live.variables[name] ? describe(name, live) : null)
        return { label: `$${name}`, type: 'variable', ...(detail ? { detail } : {}) }
      }),
      validFor: /^\$\w*$/,
    }
  }

  const options: Completion[] = [
    ...live.reference.map((op) =>
      snippetCompletion(snippetOf(op), {
        label: op.name,
        type: 'function',
        detail: op.category,
        info: () => operatorDoc(op),
      }),
    ),
    ...[...GROUPS].map((name) => ({ label: name, type: 'constant', detail: 'grouping field' })),
    ...[...locals(code(text))].map((name) => ({
      label: name,
      type: 'variable',
      detail: 'this template',
    })),
    ...WORDS.map((name) => ({ label: name, type: 'keyword' })),
  ]
  // Data fields from the catalog, once there is enough typed to search on.
  if (typed.length >= 2) {
    try {
      const fields = await catalog.searchAnywhere(typed)
      if (context.aborted) return null
      for (const field of fields)
        options.push({
          label: field.field_id,
          type: 'property',
          ...(field.dataset_id ? { detail: field.dataset_id } : {}),
          ...(field.description ? { info: field.description } : {}),
        })
    } catch {
      // Completions without fields are still completions.
    }
  }
  return { from, options }
}

/**
 * The preview's problems that name something in the text, by the exact words it writes them in:
 * a variable's own as `$name: …`, the rest as a sentence about a named operator or field. Any
 * other sentence has no place and stays below the editor.
 */
const SHAPES: { pattern: RegExp; call?: boolean }[] = [
  { pattern: /^(\$[A-Za-z_]\w*): (.+)$/ },
  { pattern: /^(\$[A-Za-z_]\w*) is chosen by the search/ },
  { pattern: /^([A-Za-z_]\w*) is not one of your operators\.$/, call: true },
  { pattern: /^([A-Za-z_]\w*) does not take \d+ inputs?\.$/, call: true },
  { pattern: /^([A-Za-z_]\w*) is not a data field in /, call: false },
]

/** Problems the preview found, drawn on the text they name; ones with no place stay below. */
function diagnose(view: EditorView, live: Live): Diagnostic[] {
  const text = view.state.doc.toString()
  const blank = code(text)
  const found: Diagnostic[] = []
  for (const m of blank.matchAll(/\.\.\./g))
    found.push({
      from: m.index,
      to: m.index + 3,
      severity: 'warning',
      message: 'Write a signal here.',
    })
  const open: number[] = []
  for (const m of blank.matchAll(/[()]/g)) {
    if (m[0] === '(') open.push(m.index)
    else if (open.pop() === undefined)
      found.push({
        from: m.index,
        to: m.index + 1,
        severity: 'error',
        message: 'Nothing opens this bracket.',
      })
  }
  for (const at of open)
    found.push({
      from: at,
      to: at + 1,
      severity: 'error',
      message: 'This bracket is never closed.',
    })
  if (live.reference.length) {
    const known = new Set(live.reference.map((o) => o.name))
    for (const m of blank.matchAll(/(?<![$\w.])([A-Za-z_]\w*)(?=\s*\()/g)) {
      const name = m[1] ?? ''
      if (!known.has(name))
        found.push({
          from: m.index,
          to: m.index + name.length,
          severity: 'error',
          message: `${name} is not one of your operators.`,
        })
    }
  }

  for (const problem of live.problems) {
    const parsed = / at (\d+)/.exec(problem)
    if (problem.startsWith("The template can't be read") && parsed) {
      const at = Math.min(Number(parsed[1]), Math.max(0, text.length - 1))
      found.push({
        from: at,
        to: Math.min(text.length, at + 1),
        severity: 'error',
        message: problem,
      })
      continue
    }
    for (const shape of SHAPES) {
      const named = shape.pattern.exec(problem)
      const name = named?.[1]
      if (!name) continue
      const message = named[2] ?? problem
      const escaped = name.replace('$', '\\$')
      const where = name.startsWith('$')
        ? `${escaped}(?!\\w)`
        : `(?<![$\\w])${escaped}(?!\\w)${shape.call ? '(?=\\s*\\()' : '(?!\\s*\\()'}`
      for (const m of blank.matchAll(new RegExp(where, 'g')))
        found.push({ from: m.index, to: m.index + name.length, severity: 'error', message })
      break
    }
  }

  const seen = new Set<string>()
  return found.filter((d) => {
    const key = `${d.from}:${d.to}:${d.message}`
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

/** Marks a change made from outside the editor, which is not the reader's typing. */
const outside = Annotation.define<boolean>()

/**
 * Asks the linter to look again though the text is unchanged: new problems arrived. Forcing it
 * is not enough, since CodeMirror only forces a lint that is already pending.
 */
const relint = StateEffect.define<null>()

function extensions(live: { current: Live }, label: string, readOnly: boolean) {
  return [
    // Shown, not edited: the colours and hovers stay, the cursor and typing go.
    ...(readOnly ? [EditorState.readOnly.of(true), EditorView.editable.of(false)] : []),
    lineNumbers(),
    highlightActiveLineGutter(),
    highlightSpecialChars(),
    history(),
    drawSelection(),
    dropCursor(),
    EditorState.allowMultipleSelections.of(true),
    indentOnInput(),
    fastExpression,
    syntaxHighlighting(darkPlus),
    bracketMatching(),
    closeBrackets(),
    autocompletion({ override: [(context) => complete(context, live.current)] }),
    rectangularSelection(),
    highlightActiveLine(),
    highlightSelectionMatches(),
    nameColours(),
    ...(readOnly ? [] : [signatures(live)]),
    hoverTooltip((view, pos) => hover(view, pos, live.current), { hoverTime: 300 }),
    linter((view) => diagnose(view, live.current), {
      delay: 250,
      needsRefresh: (update) =>
        update.transactions.some((tr) => tr.effects.some((e) => e.is(relint))),
    }),
    keymap.of([
      { key: 'Tab', run: acceptCompletion },
      ...closeBracketsKeymap,
      ...defaultKeymap,
      ...searchKeymap,
      ...historyKeymap,
      ...completionKeymap,
      ...lintKeymap,
    ]),
    EditorView.lineWrapping,
    EditorView.contentAttributes.of({ 'aria-label': label, spellcheck: 'false' }),
    EditorView.updateListener.of((update) => {
      if (update.docChanged && !update.transactions.some((tr) => tr.annotation(outside)))
        live.current.onChange(update.state.doc.toString())
    }),
    theme,
  ]
}

export function TemplateEditor({
  value,
  label = 'Template',
  readOnly = false,
  ...rest
}: Live & { value: string; label?: string; readOnly?: boolean }) {
  const host = useRef<HTMLDivElement>(null)
  const view = useRef<EditorView | null>(null)
  const live = useRef<Live>(rest)
  live.current = rest
  const first = useRef(value)
  const name = useRef(label)
  const locked = useRef(readOnly)

  useEffect(() => {
    if (!host.current) return
    const editor = new EditorView({
      parent: host.current,
      state: EditorState.create({
        doc: first.current,
        extensions: extensions(live, name.current, locked.current),
      }),
    })
    view.current = editor
    return () => {
      editor.destroy()
      view.current = null
    }
  }, [])

  // Opening a template or Reset replaces the text from outside.
  useEffect(() => {
    const editor = view.current
    if (editor && editor.state.doc.toString() !== value)
      editor.dispatch({
        changes: { from: 0, to: editor.state.doc.length, insert: value },
        annotations: outside.of(true),
      })
  }, [value])

  // The preview's problems and the operator list change what is drawn without touching the text,
  // so the linter is asked again when they arrive rather than waiting for the next keystroke.
  const { problems, reference } = rest
  // biome-ignore lint/correctness/useExhaustiveDependencies: the linter reads both through `live`
  useEffect(() => {
    const editor = view.current
    if (!editor) return
    editor.dispatch({ effects: relint.of(null) })
    forceLinting(editor)
  }, [problems, reference])

  return <div ref={host} className="overflow-hidden rounded-md border border-hairline-strong" />
}
