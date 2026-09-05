/**
 * ActiveProjectContext.test.tsx
 *
 * Tests for ActiveProjectContext: switching, localStorage persistence,
 * SSE teardown on project switch, and URL-based project sync.
 *
 * Identity source is /api/projects (projectsApi.list), not the host-wide
 * fleet registry — see D#2239. The module is mocked directly rather than
 * mocking fetch, since ActiveProjectContext only ever calls projectsApi.list().
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { useActiveProject, ActiveProjectProvider } from '../ActiveProjectContext'
import { registerEventSource, unregisterEventSource } from '../sseRegistry'
import { projectNameFromPathname } from '../../lib/urlProject'

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

vi.mock('../../api/client', () => ({
  projectsApi: {
    list: vi.fn(),
  },
}))

import { projectsApi } from '../../api/client'

const mockList = vi.mocked(projectsApi.list)

// jsdom's default URL is http://localhost/ which has port="" (no explicit port).
// The context produces "af.activeProject." in test environments. We derive the
// expected key the same way so tests stay in sync if jsdom defaults change.
const STORAGE_KEY = `af.activeProject.${window.location.port}`

// localStorage mock
const _storage: Record<string, string> = {}
const localStorageMock = {
  getItem: (k: string) => _storage[k] ?? null,
  setItem: (k: string, v: string) => { _storage[k] = v },
  removeItem: (k: string) => { delete _storage[k] },
  clear: () => { Object.keys(_storage).forEach(k => delete _storage[k]) },
}
Object.defineProperty(globalThis, 'localStorage', { value: localStorageMock, writable: true })

// Minimal test component
function TestConsumer() {
  const { activeName, setActive } = useActiveProject()
  return (
    <div>
      <div data-testid="active-name">{activeName ?? 'none'}</div>
      <button
        data-testid="switch-to-projectb"
        onClick={() => setActive('projectb')}
      >
        Switch to projectb
      </button>
    </div>
  )
}

function renderWithProvider(initialPath = '/') {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <ActiveProjectProvider>
        <TestConsumer />
      </ActiveProjectProvider>
    </MemoryRouter>
  )
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

interface FakeProject {
  id: string
  name: string
  primary?: boolean
}

function makeProject(name: string, primary = false): FakeProject {
  return { id: name, name, primary }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('ActiveProjectContext', () => {
  beforeEach(() => {
    localStorageMock.clear()
    mockList.mockReset()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('renders with null activeName before fetch resolves', () => {
    // Never resolves
    mockList.mockReturnValue(new Promise(() => undefined) as never)
    renderWithProvider()
    // activeName starts from localStorage (empty) → 'none'
    expect(screen.getByTestId('active-name').textContent).toBe('none')
  })

  it('defaults to the primary project when nothing is stored', async () => {
    mockList.mockResolvedValue([
      makeProject('autonomous-forever'),
      makeProject('gatekeep', true),
    ] as never)
    renderWithProvider()
    await waitFor(() =>
      expect(screen.getByTestId('active-name').textContent).toBe('gatekeep')
    )
    expect(localStorageMock.getItem(STORAGE_KEY)).toBe('gatekeep')
  })

  it('falls back to the first entry when no project is primary', async () => {
    mockList.mockResolvedValue([
      makeProject('autonomous-forever'),
      makeProject('projectb'),
    ] as never)
    renderWithProvider()
    await waitFor(() =>
      expect(screen.getByTestId('active-name').textContent).toBe('autonomous-forever')
    )
  })

  it('restores activeName from localStorage', async () => {
    localStorageMock.setItem(STORAGE_KEY, 'projectb')
    mockList.mockResolvedValue([
      makeProject('autonomous-forever', true),
      makeProject('projectb'),
    ] as never)
    renderWithProvider()
    await waitFor(() =>
      expect(screen.getByTestId('active-name').textContent).toBe('projectb')
    )
  })

  it('setActive persists to localStorage and updates displayed name', async () => {
    mockList.mockResolvedValue([
      makeProject('autonomous-forever', true),
      makeProject('projectb'),
    ] as never)
    const user = userEvent.setup()
    renderWithProvider()

    await waitFor(() =>
      expect(screen.getByTestId('active-name').textContent).toBe('autonomous-forever')
    )

    await act(async () => {
      await user.click(screen.getByTestId('switch-to-projectb'))
    })

    expect(screen.getByTestId('active-name').textContent).toBe('projectb')
    expect(localStorageMock.getItem(STORAGE_KEY)).toBe('projectb')
  })

  it('closes open EventSources on project switch', async () => {
    mockList.mockResolvedValue([
      makeProject('autonomous-forever', true),
      makeProject('projectb'),
    ] as never)
    const user = userEvent.setup()
    renderWithProvider()

    await waitFor(() =>
      expect(screen.getByTestId('active-name').textContent).toBe('autonomous-forever')
    )

    // Register a mock EventSource
    const closeSpy = vi.fn()
    const mockEs = { close: closeSpy } as unknown as EventSource
    registerEventSource(mockEs)

    await act(async () => {
      await user.click(screen.getByTestId('switch-to-projectb'))
    })

    expect(closeSpy).toHaveBeenCalledOnce()

    // Clean up — unregister since close() was called
    unregisterEventSource(mockEs)
  })

  it('handles fetch failure gracefully', async () => {
    mockList.mockRejectedValue(new Error('network error'))
    renderWithProvider()
    // Should render without crash — activeName stays null
    await waitFor(() =>
      expect(screen.getByTestId('active-name')).toBeTruthy()
    )
  })

  // ---------------------------------------------------------------------------
  // Port-scoped storage key tests
  // ---------------------------------------------------------------------------

  it('uses a port-scoped localStorage key (af.activeProject.<port>)', async () => {
    mockList.mockResolvedValue([makeProject('autonomous-forever', true)] as never)
    renderWithProvider()
    await waitFor(() =>
      expect(screen.getByTestId('active-name').textContent).toBe('autonomous-forever')
    )
    // Port-scoped key must be written; unscoped legacy key must NOT be written
    expect(localStorageMock.getItem('af.activeProject')).toBeNull()
    expect(localStorageMock.getItem(STORAGE_KEY)).toBe('autonomous-forever')
  })

  it('port-scoped key isolates projects across ports', async () => {
    // Simulate a different port having written its own selection. We use a
    // concrete port suffix that is distinct from the test environment's default
    // (which is "" since jsdom uses http://localhost/ with no explicit port).
    const otherPortKey = 'af.activeProject.5102'
    localStorageMock.setItem(otherPortKey, 'projectb')

    mockList.mockResolvedValue([
      makeProject('autonomous-forever', true),
      makeProject('projectb'),
    ] as never)
    renderWithProvider()

    // This instance's key (empty port in jsdom) is independent of the 5102 key
    await waitFor(() =>
      expect(screen.getByTestId('active-name').textContent).toBe('autonomous-forever')
    )
    // The other port's key is untouched
    expect(localStorageMock.getItem(otherPortKey)).toBe('projectb')
  })
})

// ---------------------------------------------------------------------------
// projectNameFromPathname unit tests
// ---------------------------------------------------------------------------

describe('projectNameFromPathname', () => {
  it('extracts project name from /project/:name/kpi', () => {
    expect(projectNameFromPathname('/project/projectb/kpi')).toBe('projectb')
  })

  it('extracts project name from /project/:name/ (trailing slash)', () => {
    expect(projectNameFromPathname('/project/projectb/')).toBe('projectb')
  })

  it('extracts project name from /project/:name (no trailing slash)', () => {
    expect(projectNameFromPathname('/project/autonomous-forever')).toBe('autonomous-forever')
  })

  it('returns null for paths without /project/ prefix', () => {
    expect(projectNameFromPathname('/kpi')).toBeNull()
    expect(projectNameFromPathname('/')).toBeNull()
    expect(projectNameFromPathname('/fleet')).toBeNull()
  })

  it('decodes percent-encoded project names', () => {
    expect(projectNameFromPathname('/project/my%20project/kpi')).toBe('my project')
  })
})

// ---------------------------------------------------------------------------
// URL-based project sync tests
// ---------------------------------------------------------------------------

describe('ActiveProjectContext — URL sync', () => {
  beforeEach(() => {
    localStorageMock.clear()
    mockList.mockReset()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('uses URL project name on mount even when localStorage says something else', async () => {
    // localStorage says "autonomous-forever"
    localStorageMock.setItem(STORAGE_KEY, 'autonomous-forever')

    mockList.mockResolvedValue([
      makeProject('autonomous-forever', true),
      makeProject('projectb'),
    ] as never)

    // URL says /project/projectb/kpi — URL wins
    renderWithProvider('/project/projectb/kpi')

    await waitFor(() =>
      expect(screen.getByTestId('active-name').textContent).toBe('projectb')
    )
  })

  it('persists URL-derived name to localStorage', async () => {
    mockList.mockResolvedValue([makeProject('projectb', true)] as never)

    renderWithProvider('/project/projectb/kpi')

    await waitFor(() =>
      expect(screen.getByTestId('active-name').textContent).toBe('projectb')
    )

    expect(localStorageMock.getItem(STORAGE_KEY)).toBe('projectb')
  })

  it('falls back to localStorage when URL has no project segment', async () => {
    localStorageMock.setItem(STORAGE_KEY, 'autonomous-forever')

    mockList.mockResolvedValue([
      makeProject('autonomous-forever', true),
      makeProject('projectb'),
    ] as never)

    // Path without /project/:name
    renderWithProvider('/kpi')

    await waitFor(() =>
      expect(screen.getByTestId('active-name').textContent).toBe('autonomous-forever')
    )
  })
})
