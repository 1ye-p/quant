import '@testing-library/jest-dom'

// vitest@4 + jsdom@29 in this environment exposes a keyless plain object as
// window.localStorage (no Storage methods — getItem/clear are undefined).
// Components shouldn't need per-call try/catch just to survive tests, so
// install a spec-compliant in-memory Storage whenever the native one is broken.
if (typeof localStorage === 'undefined' || typeof localStorage.getItem !== 'function') {
  const store = new Map<string, string>()
  const storage: Storage = {
    get length() {
      return store.size
    },
    key(index: number) {
      return [...store.keys()][index] ?? null
    },
    getItem(key: string) {
      return store.has(key) ? store.get(key)! : null
    },
    setItem(key: string, value: string) {
      store.set(key, String(value))
    },
    removeItem(key: string) {
      store.delete(key)
    },
    clear() {
      store.clear()
    },
  }
  Object.defineProperty(window, 'localStorage', {
    value: storage,
    configurable: true,
    writable: true,
  })
  // The bare `localStorage` global may have been copied as an own property
  // before the swap above — realign it with the fixed window.localStorage.
  try {
    if (globalThis.localStorage !== storage) {
      ;(globalThis as { localStorage?: Storage }).localStorage = storage
    }
  } catch {
    // read-only global — window.localStorage already covers jsdom tests
  }
}
