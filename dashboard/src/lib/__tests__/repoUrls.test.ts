import { describe, it, expect } from 'vitest'
import { normalizeRepoSlug, repoUrl, discussionUrl, pullUrl } from '../repoUrls'

describe('normalizeRepoSlug', () => {
  it('accepts a bare owner/name slug', () => {
    expect(normalizeRepoSlug('fulcrumaxe/gatekeep')).toBe('fulcrumaxe/gatekeep')
  })

  it('strips a full github.com URL prefix', () => {
    expect(normalizeRepoSlug('https://github.com/fulcrumaxe/gatekeep')).toBe('fulcrumaxe/gatekeep')
  })

  it('strips a bare github.com prefix (no scheme)', () => {
    expect(normalizeRepoSlug('github.com/fulcrumaxe/gatekeep')).toBe('fulcrumaxe/gatekeep')
  })

  it('strips a trailing slash', () => {
    expect(normalizeRepoSlug('fulcrumaxe/gatekeep/')).toBe('fulcrumaxe/gatekeep')
  })

  it('strips a trailing .git', () => {
    expect(normalizeRepoSlug('fulcrumaxe/gatekeep.git')).toBe('fulcrumaxe/gatekeep')
  })

  it('null returns null', () => {
    expect(normalizeRepoSlug(null)).toBeNull()
  })

  it('undefined returns null', () => {
    expect(normalizeRepoSlug(undefined)).toBeNull()
  })

  it('empty string returns null', () => {
    expect(normalizeRepoSlug('')).toBeNull()
  })

  it('a bare name with no slash returns null', () => {
    expect(normalizeRepoSlug('owner')).toBeNull()
  })

  it('an extra path segment returns null', () => {
    expect(normalizeRepoSlug('owner/name/extra')).toBeNull()
  })
})

describe('repoUrl / discussionUrl / pullUrl', () => {
  it('repoUrl builds a github.com URL for a valid slug', () => {
    expect(repoUrl('fulcrumaxe/gatekeep')).toBe('https://github.com/fulcrumaxe/gatekeep')
  })

  it('repoUrl returns null for an invalid slug', () => {
    expect(repoUrl('owner/name/extra')).toBeNull()
  })

  it('discussionUrl builds a discussions link for a valid slug', () => {
    expect(discussionUrl('owner/name', 7)).toBe('https://github.com/owner/name/discussions/7')
  })

  it('discussionUrl returns null for each invalid input', () => {
    expect(discussionUrl(null, 7)).toBeNull()
    expect(discussionUrl(undefined, 7)).toBeNull()
    expect(discussionUrl('', 7)).toBeNull()
    expect(discussionUrl('owner', 7)).toBeNull()
    expect(discussionUrl('owner/name/extra', 7)).toBeNull()
  })

  it('pullUrl builds a pull link for a valid slug', () => {
    expect(pullUrl('owner/name', 42)).toBe('https://github.com/owner/name/pull/42')
  })

  it('pullUrl returns null for each invalid input', () => {
    expect(pullUrl(null, 42)).toBeNull()
    expect(pullUrl(undefined, 42)).toBeNull()
    expect(pullUrl('', 42)).toBeNull()
    expect(pullUrl('owner', 42)).toBeNull()
    expect(pullUrl('owner/name/extra', 42)).toBeNull()
  })
})
