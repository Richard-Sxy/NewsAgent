export const RECEIPT_POLL_INTERVAL_MS = 3_000
export const RECEIPT_POLL_MAX_FAILURES = 3

export interface EntityPollState<T> {
  value: T | null
  loading: boolean
  degraded: boolean
  error: string | null
}

export interface PollEnvironment {
  visible: () => boolean
  onVisibility: (callback: () => void) => () => void
  schedule: (callback: () => void, delay: number) => unknown
  cancel: (handle: unknown) => void
}

export interface EntitySubscription {
  refresh: () => Promise<void>
  unsubscribe: () => void
}

const browserEnvironment: PollEnvironment = {
  visible: () => typeof document === 'undefined' || document.visibilityState !== 'hidden',
  onVisibility: (callback) => {
    if (typeof document === 'undefined') return () => {}
    document.addEventListener('visibilitychange', callback)
    return () => document.removeEventListener('visibilitychange', callback)
  },
  schedule: (callback, delay) => setTimeout(callback, delay),
  cancel: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
}

/** One in-flight read and timer per entity, shared by all historical cards in the current SPA. */
export function createEntityPoller<T>(options: {
  read: (id: string, seed: T) => Promise<T>
  terminal: (value: T) => boolean
  newer: (candidate: T, current: T) => boolean
  environment?: PollEnvironment
  intervalMs?: number
}) {
  const environment = options.environment ?? browserEnvironment
  const intervalMs = options.intervalMs ?? RECEIPT_POLL_INTERVAL_MS
  interface Entry {
    seed: T
    state: EntityPollState<T>
    listeners: Set<(state: EntityPollState<T>) => void>
    timer: unknown | null
    inFlight: Promise<void> | null
    failures: number
  }
  const entries = new Map<string, Entry>()
  let unlisten: (() => void) | null = null

  function emit(entry: Entry): void {
    for (const listener of entry.listeners) listener({ ...entry.state })
  }

  function cancelTimer(entry: Entry): void {
    if (entry.timer !== null) environment.cancel(entry.timer)
    entry.timer = null
  }

  function active(entry: Entry): boolean {
    return entry.listeners.size > 0 && environment.visible() && !entry.state.degraded
      && !options.terminal(entry.state.value ?? entry.seed)
  }

  function cleanup(id: string, entry: Entry): void {
    if (entry.listeners.size || entry.inFlight) return
    entries.delete(id)
    if (!entries.size) { unlisten?.(); unlisten = null }
  }

  function schedule(id: string, entry: Entry): void {
    cancelTimer(entry)
    if (!active(entry)) return
    entry.timer = environment.schedule(() => { entry.timer = null; void poll(id, entry) }, intervalMs)
  }

  async function poll(id: string, entry: Entry, manual = false): Promise<void> {
    if (entry.inFlight) return entry.inFlight
    if (!manual && !active(entry) || !entry.listeners.size || !environment.visible()) return
    cancelTimer(entry)
    if (manual) { entry.failures = 0; entry.state.degraded = false }
    entry.state.loading = true
    emit(entry)
    // Yield once so the promise is registered before a synchronously rejected reader settles.
    entry.inFlight = Promise.resolve().then(() => options.read(id, entry.seed)).then((value) => {
      entry.state.value = options.newer(entry.seed, value) ? entry.seed : value
      entry.failures = 0
      entry.state.error = null
      entry.state.degraded = false
    }, () => {
      entry.failures++
      entry.state.error = '读取任务状态失败'
      entry.state.degraded = entry.failures >= RECEIPT_POLL_MAX_FAILURES
    }).finally(() => {
      entry.inFlight = null
      entry.state.loading = false
      emit(entry)
      schedule(id, entry)
      cleanup(id, entry)
    })
    return entry.inFlight
  }

  function onVisibility(): void {
    for (const [id, entry] of entries) {
      cancelTimer(entry)
      if (active(entry)) void poll(id, entry)
    }
  }

  return {
    subscribe(id: string, seed: T, listener: (state: EntityPollState<T>) => void): EntitySubscription {
      let entry = entries.get(id)
      if (!entry) {
        entry = { seed, state: { value: null, loading: false, degraded: false, error: null },
          listeners: new Set(), timer: null, inFlight: null, failures: 0 }
        entries.set(id, entry)
      } else if (options.newer(seed, entry.state.value ?? entry.seed)) {
        entry.seed = seed
        // A newer saved snapshot supersedes an older live read without rewriting any tool result.
        if (entry.state.value) entry.state.value = seed
        entry.failures = 0
        entry.state.degraded = false
      }
      entry.listeners.add(listener)
      if (!unlisten) unlisten = environment.onVisibility(onVisibility)
      listener({ ...entry.state })
      if (active(entry) && entry.timer === null) void poll(id, entry)
      const subscribed = entry
      let stopped = false
      return {
        refresh: () => stopped ? Promise.resolve() : poll(id, subscribed, true),
        unsubscribe: () => {
          stopped = true
          subscribed.listeners.delete(listener)
          if (!subscribed.listeners.size) cancelTimer(subscribed)
          if (![...entries.values()].some((entry) => entry.listeners.size)) { unlisten?.(); unlisten = null }
          cleanup(id, subscribed)
        },
      }
    },
  }
}
