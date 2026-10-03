/** Choose which prompt a lab or the Assistant sends: the built-in, or one saved in LLM Prompts. */

import { Link } from '@tanstack/react-router'
import { PencilIcon } from 'lucide-react'
import { Button } from '@/ui/kit'
import { Select } from '@/ui/overlay'
import { promptKey, usePromptLibrary } from './api'

/**
 * The saved prompt `id` still names, or null for the built-in. A deleted prompt falls back to
 * the built-in once the library has loaded, so a stale choice never reaches the backend.
 */
export function useChosenPrompt(kind: string, id: number | null): number | null {
  const library = usePromptLibrary()
  if (id === null || !library.data) return id
  return library.data.prompts.some((p) => p.id === id && p.kind === kind) ? id : null
}

export function PromptPicker({
  kind,
  value,
  onChange,
  disabled,
  className,
}: {
  kind: string
  value: number | null
  onChange: (id: number | null) => void
  disabled?: boolean
  className?: string
}) {
  const library = usePromptLibrary()
  const mine = (library.data?.prompts ?? []).filter((p) => p.kind === kind)
  const chosen = mine.find((p) => p.id === value) ?? mine.find((p) => p.id === null)
  const items = mine.map((p) => ({
    value: promptKey(p),
    label: p.id === null ? `Built-in · ${p.name}` : p.name,
  }))

  return (
    <div className="flex min-w-0 items-center gap-1.5">
      <Select
        label="Prompt"
        className={className ?? 'max-w-72'}
        items={items}
        value={chosen ? promptKey(chosen) : null}
        disabled={disabled || items.length === 0}
        onChange={(key) => onChange(key === kind ? null : Number(key))}
      />
      <Button
        variant="ghost"
        size="icon-sm"
        aria-label="Edit in LLM Prompts"
        title="Edit in LLM Prompts"
        render={<Link to="/prompts" search={{ open: chosen?.id ?? kind }} />}
      >
        <PencilIcon />
      </Button>
    </div>
  )
}
