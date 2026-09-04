/**
 * sseRegistry.ts — thin registry for open SSE EventSource instances.
 *
 * When the user switches projects, ActiveProjectContext calls closeAll()
 * to tear down any SSE connections before re-opening them scoped to the
 * new project. Components that open an EventSource should call register()
 * on open and unregister() on close so the registry stays accurate.
 *
 * Kept in a separate file from ActiveProjectContext.tsx so the context
 * file can satisfy react-refresh/only-export-components (which requires
 * component-only exports).
 */

const _openSources: Set<EventSource> = new Set()

export function registerEventSource(es: EventSource): void {
  _openSources.add(es)
}

export function unregisterEventSource(es: EventSource): void {
  _openSources.delete(es)
}

export function closeAllEventSources(): void {
  for (const es of _openSources) {
    try {
      es.close()
    } catch {
      // ignore
    }
  }
  _openSources.clear()
}
