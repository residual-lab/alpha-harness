/**
 * Copies text made in a Web Worker: thousands of Alphas take seconds to format, and the page
 * would freeze for all of it on the main thread.
 *
 * Call it from the click itself. WebKit lets a page write the clipboard only while it handles
 * the click, so the write starts at once and waits on the text, which a ClipboardItem takes as
 * a promise.
 */
import { errorMessage } from '@/api/http'

export function copyFromWorker<F extends (...args: never[]) => string | Promise<string>>(
  worker: Worker,
  ...args: Parameters<F>
): Promise<void> {
  const text = new Promise<Blob>((resolve, reject) => {
    worker.onmessage = ({ data }: MessageEvent<Blob | string>) =>
      typeof data === 'string' ? reject(new Error(data)) : resolve(data)
    worker.onerror = (event) => reject(new Error(event.message || 'Could not start the copy.'))
    worker.postMessage(args)
  }).finally(() => worker.terminate())
  // Still called in the click, but a browser without the API rejects rather than throws.
  const write = new Promise<void>((resolve, reject) => {
    navigator.clipboard.write([new ClipboardItem({ 'text/plain': text })]).then(resolve, reject)
  })
  // A failed text reports its own reason, not the vaguer one the write fails with after it.
  return Promise.all([text, write]).then(() => undefined)
}

/**
 * Copies text that waits on `work`, such as a request. Call it from the click itself: the
 * write starts at once, since WebKit refuses one that follows an await. Resolves to what `work`
 * did and rejects with its error, so the caller reports a failure once.
 */
export function copyText<T>(work: () => Promise<T>, toText: (value: T) => string): Promise<T> {
  const result = new Promise<T>((resolve) => resolve(work()))
  if (typeof ClipboardItem === 'undefined' || !navigator.clipboard?.write) {
    return result.then(async (value) => {
      await navigator.clipboard.writeText(toText(value))
      return value
    })
  }
  const blob = result.then((value) => new Blob([toText(value)], { type: 'text/plain' }))
  const write = new Promise<void>((resolve, reject) => {
    navigator.clipboard.write([new ClipboardItem({ 'text/plain': blob })]).then(resolve, reject)
  })
  // A failed work reports its own reason, not the vaguer one the write fails with after it.
  return Promise.all([blob, write]).then(() => result)
}

/** A worker's half: answers each message with what `make` writes from it, as a Blob. */
export function serveText<A extends unknown[]>(make: (...args: A) => string | Promise<string>) {
  // A dedicated worker's global scope posts and receives like the Worker that started it.
  const scope = self as unknown as Worker
  scope.onmessage = async ({ data }: MessageEvent<A>) => {
    try {
      scope.postMessage(new Blob([await make(...data)], { type: 'text/plain' }))
    } catch (error) {
      scope.postMessage(errorMessage(error))
    }
  }
}
