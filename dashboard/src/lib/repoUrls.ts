/**
 * repoUrls.ts — pure slug normalization + GitHub URL builders.
 *
 * No React, no fetching — every export here is a pure function of its
 * arguments so it's unit-testable without mocking anything.
 *
 * Returning null (rather than a partial or guessed URL) is the point: a
 * caller that gets null must render plain text, never a link built from a
 * guessed repository (D#2234 — every link used to hardcode a repo that
 * wasn't the adopter's).
 */

const SLUG_RE = /^[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+$/

/**
 * Normalize a repo value into a clean "owner/name" slug, or null.
 *
 * Accepts a bare slug ("owner/name") or a full GitHub URL
 * ("https://github.com/owner/name", "github.com/owner/name"), trimming a
 * trailing slash or ".git". Anything that isn't exactly one owner and one
 * name segment — null, undefined, empty, a bare name with no slash, or an
 * extra path segment — returns null.
 */
export function normalizeRepoSlug(repo: string | null | undefined): string | null {
  if (!repo) return null
  let slug = repo.trim()
  if (!slug) return null

  slug = slug.replace(/^https?:\/\/github\.com\//i, '').replace(/^github\.com\//i, '')
  slug = slug.replace(/\/+$/, '').replace(/\.git$/i, '')

  return SLUG_RE.test(slug) ? slug : null
}

/** `https://github.com/<slug>`, or null when *repo* doesn't normalize to a clean slug. */
export function repoUrl(repo: string | null | undefined): string | null {
  const slug = normalizeRepoSlug(repo)
  return slug ? `https://github.com/${slug}` : null
}

/** `https://github.com/<slug>/discussions/<n>`, or null. */
export function discussionUrl(repo: string | null | undefined, n: number): string | null {
  const base = repoUrl(repo)
  return base ? `${base}/discussions/${n}` : null
}

/** `https://github.com/<slug>/pull/<n>`, or null. */
export function pullUrl(repo: string | null | undefined, n: number): string | null {
  const base = repoUrl(repo)
  return base ? `${base}/pull/${n}` : null
}
