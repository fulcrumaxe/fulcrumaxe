import { describe, it, expect } from 'vitest'
import { projectNameFromPathname } from '../urlProject'

describe('projectNameFromPathname — matching paths', () => {
  it('extracts project name from /project/:name/subpath', () => {
    expect(projectNameFromPathname('/project/projectb/kpi')).toBe('projectb')
  })

  it('extracts project name from /project/:name/ (trailing slash)', () => {
    expect(projectNameFromPathname('/project/projectb/')).toBe('projectb')
  })

  it('extracts project name from /project/:name (no trailing slash)', () => {
    expect(projectNameFromPathname('/project/autonomous-forever')).toBe('autonomous-forever')
  })

  it('extracts project with hyphenated name', () => {
    expect(projectNameFromPathname('/project/my-cool-project/stats')).toBe('my-cool-project')
  })

  it('extracts project with underscored name', () => {
    expect(projectNameFromPathname('/project/my_project/overview')).toBe('my_project')
  })

  it('round-trips: extract then reconstruct path', () => {
    const name = projectNameFromPathname('/project/myapp/dashboard')
    expect(`/project/${name}/dashboard`).toBe('/project/myapp/dashboard')
  })
})

describe('projectNameFromPathname — URL encoding', () => {
  it('decodes percent-encoded characters in project name', () => {
    expect(projectNameFromPathname('/project/my%20project/stats')).toBe('my project')
  })

  it('decodes encoded slashes (edge case)', () => {
    // %2F encodes a slash — the regex [^/]+ stops at real slashes only, so %2F is captured as-is and decoded
    expect(projectNameFromPathname('/project/foo%2Fbar/stats')).toBe('foo/bar')
  })
})

describe('projectNameFromPathname — non-matching paths', () => {
  it('returns null for root path', () => {
    expect(projectNameFromPathname('/')).toBeNull()
  })

  it('returns null for /kpi path (no project segment)', () => {
    expect(projectNameFromPathname('/kpi')).toBeNull()
  })

  it('returns null for /project path alone (no name segment)', () => {
    // /project alone has no name after it
    expect(projectNameFromPathname('/project')).toBeNull()
  })

  it('returns null for /project/ with no name', () => {
    expect(projectNameFromPathname('/project/')).toBeNull()
  })

  it('returns null for empty string', () => {
    expect(projectNameFromPathname('')).toBeNull()
  })

  it('returns null for unrelated path', () => {
    expect(projectNameFromPathname('/dashboard/stats')).toBeNull()
  })

  it('returns null for path that starts with something other than /project/', () => {
    expect(projectNameFromPathname('/notproject/projectb/kpi')).toBeNull()
  })
})
