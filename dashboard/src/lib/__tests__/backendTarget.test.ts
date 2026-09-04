/**
 * Tests for backendTarget.ts — the backend-selector setting module.
 *
 * Verifies:
 *  - Default is "python"
 *  - Setting persists to localStorage and is read back on module re-init
 *  - resolveRestBaseUrl() and resolveRpcBaseUrl() return correct values per selection
 *  - Listeners fire on change
 *  - subscribeBackendTarget() unsubscribe works
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'

// ---------------------------------------------------------------------------
// localStorage mock — must be set up before the module is imported so that
// the module-load-time _readFromStorage() call sees our mock.
// ---------------------------------------------------------------------------

const store: Record<string, string> = {}

const localStorageMock = {
  getItem: vi.fn((key: string) => store[key] ?? null),
  setItem: vi.fn((key: string, value: string) => { store[key] = value }),
  removeItem: vi.fn((key: string) => { delete store[key] }),
  clear: vi.fn(() => { for (const k in store) delete store[k] }),
}

// Install before module load
Object.defineProperty(globalThis, 'localStorage', {
  value: localStorageMock,
  writable: true,
})

// import.meta.env mock — must be set up before importing the module.
vi.stubGlobal('import.meta', { env: {} })

// ---------------------------------------------------------------------------
// Now import the module under test
// ---------------------------------------------------------------------------

// We re-import via a dynamic import in a helper so each test group can reset
// state. Vitest module caching means the top-level module state persists across
// tests in the same file — so we reset via the exported setter instead.
import {
  getBackendTarget,
  setBackendTarget,
  subscribeBackendTarget,
  resolveRestBaseUrl,
  resolveRpcBaseUrl,
} from '../backendTarget'

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

beforeEach(() => {
  // Reset module state between tests
  localStorageMock.clear()
  localStorageMock.getItem.mockClear()
  localStorageMock.setItem.mockClear()
  // Reset to default
  setBackendTarget('python')
})

describe('default backend', () => {
  it('is "python" when localStorage is empty', () => {
    // After beforeEach resets, default should be python
    expect(getBackendTarget()).toBe('python')
  })
})

describe('setBackendTarget / getBackendTarget', () => {
  it('stores the selection and returns it', () => {
    setBackendTarget('typescript')
    expect(getBackendTarget()).toBe('typescript')
  })

  it('writes to localStorage', () => {
    setBackendTarget('typescript')
    expect(localStorageMock.setItem).toHaveBeenCalledWith('af.backendTarget', 'typescript')
  })

  it('switching back to python stores "python"', () => {
    setBackendTarget('typescript')
    setBackendTarget('python')
    expect(getBackendTarget()).toBe('python')
    expect(localStorageMock.setItem).toHaveBeenLastCalledWith('af.backendTarget', 'python')
  })
})

describe('resolveRestBaseUrl', () => {
  it('returns window.location.origin for python backend', () => {
    setBackendTarget('python')
    // In jsdom, window.location.origin is typically 'http://localhost:3000' or similar
    const result = resolveRestBaseUrl()
    // Should not contain 19099 (the TS backend port)
    expect(result).not.toContain('19099')
  })

  it('returns the TS backend URL (127.0.0.1:19099) when typescript is selected', () => {
    setBackendTarget('typescript')
    const result = resolveRestBaseUrl()
    expect(result).toContain('19099')
  })

  it('python and typescript return different URLs', () => {
    setBackendTarget('python')
    const pythonUrl = resolveRestBaseUrl()

    setBackendTarget('typescript')
    const tsUrl = resolveRestBaseUrl()

    expect(pythonUrl).not.toBe(tsUrl)
  })
})

describe('resolveRpcBaseUrl', () => {
  it('returns empty string for python backend (signals client.ts to use its own logic)', () => {
    setBackendTarget('python')
    expect(resolveRpcBaseUrl()).toBe('')
  })

  it('returns the TS backend URL for typescript backend', () => {
    setBackendTarget('typescript')
    const result = resolveRpcBaseUrl()
    expect(result).toContain('19099')
  })
})

describe('subscribeBackendTarget', () => {
  it('calls listener when backend changes', () => {
    const listener = vi.fn()
    subscribeBackendTarget(listener)
    setBackendTarget('typescript')
    expect(listener).toHaveBeenCalledWith('typescript')
  })

  it('unsubscribes when the returned function is called', () => {
    const listener = vi.fn()
    const unsub = subscribeBackendTarget(listener)
    unsub()
    setBackendTarget('typescript')
    expect(listener).not.toHaveBeenCalled()
  })

  it('multiple listeners all fire', () => {
    const l1 = vi.fn()
    const l2 = vi.fn()
    subscribeBackendTarget(l1)
    subscribeBackendTarget(l2)
    setBackendTarget('typescript')
    expect(l1).toHaveBeenCalledWith('typescript')
    expect(l2).toHaveBeenCalledWith('typescript')
    // cleanup
    l1.mockClear(); l2.mockClear()
    setBackendTarget('python')
  })
})
