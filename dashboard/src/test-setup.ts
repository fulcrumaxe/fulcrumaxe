import '@testing-library/jest-dom'
import { afterEach, vi } from 'vitest'
import { cleanup } from '@testing-library/react'

if (typeof navigator !== 'undefined' && !navigator.clipboard) {
  Object.defineProperty(navigator, 'clipboard', {
    value: {
      readText: async () => '',
      writeText: async () => {},
    },
    configurable: true,
  })
}

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.clearAllTimers()
  vi.restoreAllMocks()
})
