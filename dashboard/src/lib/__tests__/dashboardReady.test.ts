import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { isDashboardReady, DASHBOARD_READY_SCRIPT } from '../dashboardReady'

const __dirname = dirname(fileURLToPath(import.meta.url))

// D#2549: this predicate exists because two browser-tester passes were
// measured on screenshots of a still-loading page ("Loading metrics…",
// "0 of N populated"). These cases demonstrate both directions required by
// the Discussion's acceptance criteria — false on a still-loading page, true
// on a populated one — and the "stalled page" case is the mutation-evidence
// demonstration: pointed at a page that never finishes loading, the
// predicate must return false (go red), never true.

function docFromHtml(html: string): Document {
  const parser = new DOMParser()
  return parser.parseFromString(html, 'text/html')
}

describe('isDashboardReady — still-loading page (false)', () => {
  it('returns false while a "Loading metrics…" message is showing and the grid is absent', () => {
    const doc = docFromHtml(`
      <div>
        <p>Per-merge metrics — 0 of 17 populated</p>
        <div>Loading metrics…</div>
      </div>
    `)
    expect(isDashboardReady(doc)).toBe(false)
  })

  it('returns false — deliberately stalled page — grid mounted but empty, still loading', () => {
    // Mirrors the real regression: the grid container exists (React has
    // mounted it) but the fetch has not resolved, so it has no children.
    const doc = docFromHtml(`
      <div>
        <div data-testid="stats-grid"></div>
        <div>Loading metrics…</div>
      </div>
    `)
    expect(isDashboardReady(doc)).toBe(false)
  })

  it('returns false when no recognizable grid/list container has rendered at all', () => {
    const doc = docFromHtml(`<div><p>Team Stats</p></div>`)
    expect(isDashboardReady(doc)).toBe(false)
  })

  it('returns false when the grid already has children but a loading indicator is still present (D#2549 review: the /loading/i blind spot)', () => {
    // The two "loading" fixtures above both have an EMPTY grid, so they
    // return false via the containers-empty branch, not the /loading/i
    // branch — a mutation that deletes only the /loading/i check from ONE
    // copy of this predicate does not redden either of them (verified: all
    // 7 pre-existing tests still passed with that mutation applied to only
    // the DASHBOARD_READY_SCRIPT literal). This fixture has a populated
    // grid (stale content from a previous render) with a loading indicator
    // shown over it, so the /loading/i check is the ONLY thing making it
    // false.
    const doc = docFromHtml(`
      <div>
        <div data-testid="stats-grid">
          <div data-testid="metric-tile">stale-value-from-last-render</div>
        </div>
        <div>Loading metrics…</div>
      </div>
    `)
    expect(isDashboardReady(doc)).toBe(false)
  })
})

describe('isDashboardReady — populated page (true)', () => {
  it('returns true once the loading message is gone and the grid has children', () => {
    const doc = docFromHtml(`
      <div>
        <p>Per-merge metrics — 12 of 12 populated</p>
        <div data-testid="stats-grid">
          <div data-testid="metric-tile">loop_iteration_duration_seconds</div>
          <div data-testid="metric-tile">time_to_merge_seconds</div>
        </div>
      </div>
    `)
    expect(isDashboardReady(doc)).toBe(true)
  })

  it('also recognizes a "-list" container, not only "-grid"', () => {
    const doc = docFromHtml(`
      <div data-testid="active-agents-list">
        <div>agent-1</div>
      </div>
    `)
    expect(isDashboardReady(doc)).toBe(true)
  })
})

describe('isDashboardReady — edge cases', () => {
  it('returns false for a document with no body', () => {
    const doc = docFromHtml('')
    // jsdom always synthesizes a <body>, so force the case explicitly to
    // cover a caller that hands in a detached/partial document.
    Object.defineProperty(doc, 'body', { value: null, configurable: true })
    expect(isDashboardReady(doc)).toBe(false)
  })
})

describe('DASHBOARD_READY_SCRIPT — every copy stays in lockstep', () => {
  // D#2549 review: the parity test used to compare only 2 of the 3 copies
  // that exist — isDashboardReady() and the DASHBOARD_READY_SCRIPT literal
  // in this file. The THIRD copy, embedded as markdown in
  // .claude/agents/browser-tester.md, is the one an actual browser-tester
  // run evaluates, and it had zero automated drift protection. These tests
  // cover all three, against fixtures that include the one above that
  // specifically depends on the /loading/i branch (without it, stripping
  // /loading/i from only one copy passed every pre-existing test).
  const loading = docFromHtml(`<div data-testid="stats-grid"></div><div>Loading metrics…</div>`)
  const stalledWithChildren = docFromHtml(`
    <div>
      <div data-testid="stats-grid"><div data-testid="metric-tile">stale</div></div>
      <div>Loading metrics…</div>
    </div>
  `)
  const populated = docFromHtml(
    `<div data-testid="stats-grid"><div data-testid="metric-tile">x</div></div>`,
  )
  const fixtures = [loading, stalledWithChildren, populated]

  const runAgainst = (script: string, doc: Document) =>
    new Function('document', `return ${script}`)(doc)

  it('DASHBOARD_READY_SCRIPT is a self-contained expression with no imports, matching isDashboardReady on every fixture', () => {
    expect(DASHBOARD_READY_SCRIPT).not.toContain('import ')
    expect(DASHBOARD_READY_SCRIPT).not.toContain('export ')

    for (const doc of fixtures) {
      expect(runAgainst(DASHBOARD_READY_SCRIPT, doc)).toBe(isDashboardReady(doc))
    }
    expect(runAgainst(DASHBOARD_READY_SCRIPT, loading)).toBe(false)
    expect(runAgainst(DASHBOARD_READY_SCRIPT, stalledWithChildren)).toBe(false)
    expect(runAgainst(DASHBOARD_READY_SCRIPT, populated)).toBe(true)
  })

  it('the browser-tester.md template copy is present, byte-identical to DASHBOARD_READY_SCRIPT, and behaviorally in parity on every fixture', () => {
    const mdPath = join(__dirname, '../../../../.claude/agents/browser-tester.md')
    const md = readFileSync(mdPath, 'utf8')
    const match = md.match(
      /<!-- DASHBOARD_READY_SCRIPT:BEGIN -->\n([\s\S]*?)\n\s*<!-- DASHBOARD_READY_SCRIPT:END -->/,
    )
    expect(match).not.toBeNull()
    const templateScript = match![1]

    // True byte identity (modulo the block's markdown indentation), not
    // just behavioral parity on today's fixtures — a dedent-and-compare
    // catches a drift a behavioral check alone could miss (two expressions
    // with different source text that happen to agree on these fixtures).
    const dedent = (s: string) => {
      const lines = s.split('\n')
      const indents = lines
        .filter(l => l.trim().length > 0)
        .map(l => l.match(/^\s*/)![0].length)
      const min = indents.length > 0 ? Math.min(...indents) : 0
      return lines.map(l => l.slice(min)).join('\n')
    }
    expect(dedent(templateScript)).toBe(dedent(DASHBOARD_READY_SCRIPT))

    for (const doc of fixtures) {
      expect(runAgainst(templateScript, doc)).toBe(isDashboardReady(doc))
    }
  })
})
