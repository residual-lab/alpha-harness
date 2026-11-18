/**
 * A short stand-in for a long list in a query key: how many items, and a 64-bit hash of their
 * exact JSON. TanStack hashes the whole key on every render of its query, which for thousands
 * of Alpha ids is tens of kilobytes of JSON each time; a fingerprint is worked out once per list.
 */
export function fingerprint(items: readonly unknown[]): string {
  const json = JSON.stringify(items)
  // cyrb53 with both of its 32-bit lanes kept: 64 bits, so no two lists a cache holds collide.
  let a = 0xdeadbeef
  let b = 0x41c6ce57
  for (let i = 0; i < json.length; i++) {
    const c = json.charCodeAt(i)
    a = Math.imul(a ^ c, 2654435761)
    b = Math.imul(b ^ c, 1597334677)
  }
  a = Math.imul(a ^ (a >>> 16), 2246822507) ^ Math.imul(b ^ (b >>> 13), 3266489909)
  b = Math.imul(b ^ (b >>> 16), 2246822507) ^ Math.imul(a ^ (a >>> 13), 3266489909)
  const hex = (h: number) => (h >>> 0).toString(16).padStart(8, '0')
  return `${items.length}:${hex(b)}${hex(a)}`
}
