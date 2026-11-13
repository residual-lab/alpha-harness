import { afterEach, describe, expect, it, vi } from 'vitest'
import { copyText } from './clipboard'

class FakeItem {
  types: Record<string, Promise<Blob>>
  constructor(types: Record<string, Promise<Blob>>) {
    this.types = types
  }
}

function stub(clipboard: Partial<Clipboard>, withItem = true) {
  vi.stubGlobal('navigator', { clipboard })
  if (withItem) vi.stubGlobal('ClipboardItem', FakeItem)
  else vi.stubGlobal('ClipboardItem', undefined)
}

afterEach(() => vi.unstubAllGlobals())

describe('copyText', () => {
  it('starts the write in the click and fills it with the text', async () => {
    let written: FakeItem | undefined
    const write = vi.fn(async (items: ClipboardItem[]) => {
      written = items[0] as unknown as FakeItem
      await written.types['text/plain']
    })
    stub({ write })
    const done = copyText(
      async () => ({ text: 'a\nb', n: 2 }),
      (v) => v.text,
    )
    expect(write).toHaveBeenCalledTimes(1)
    expect(await done).toEqual({ text: 'a\nb', n: 2 })
    expect(await written?.types['text/plain']?.then((b) => b.text())).toBe('a\nb')
  })

  it('rejects once with the work error', async () => {
    const write = vi.fn(async (items: ClipboardItem[]) => {
      await (items[0] as unknown as FakeItem).types['text/plain']
    })
    stub({ write })
    const failure = new Error('422')
    await expect(
      copyText(
        () => Promise.reject(failure),
        () => '',
      ),
    ).rejects.toBe(failure)
  })

  it('falls back to writeText without ClipboardItem', async () => {
    const writeText = vi.fn(async () => undefined)
    stub({ writeText }, false)
    await copyText(
      async () => 'hello',
      (t) => t,
    )
    expect(writeText).toHaveBeenCalledWith('hello')
  })

  it('does not write on fallback when the work fails', async () => {
    const writeText = vi.fn(async () => undefined)
    stub({ writeText }, false)
    await expect(
      copyText(
        () => Promise.reject(new Error('boom')),
        () => '',
      ),
    ).rejects.toThrow('boom')
    expect(writeText).not.toHaveBeenCalled()
  })
})
