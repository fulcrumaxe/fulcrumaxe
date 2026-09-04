/**
 * PRDetailPage — read-only view of a single PR joining meta, linked
 * Discussion, quality score, and cost breakdown.
 *
 * Route: /pr/:number
 * Lazy-loaded from App.tsx so it does not affect the main bundle.
 */

import React, { useEffect, useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { fetchPrDetail } from '../api/pr'
import type { PrDetail } from '../api/types'
import PrMetaCard from './pr/PrMetaCard'
import DiscussionLinkCard from './pr/DiscussionLinkCard'
import QualityScoreCard from './pr/QualityScoreCard'
import CostBreakdownCard from './pr/CostBreakdownCard'

type PageState =
  | { phase: 'loading' }
  | { phase: 'not_found' }
  | { phase: 'error'; message: string }
  | { phase: 'done'; data: PrDetail }

export default function PRDetailPage() {
  const { number } = useParams<{ number: string }>()
  const prNumber = parseInt(number ?? '0', 10)
  const [state, setState] = useState<PageState>({ phase: 'loading' })

  useEffect(() => {
    if (!prNumber || isNaN(prNumber)) {
      setState({ phase: 'error', message: 'Invalid PR number' })
      return
    }

    let cancelled = false
    fetchPrDetail(prNumber)
      .then(result => {
        if (cancelled) return
        if (result.error === 'not_found') {
          setState({ phase: 'not_found' })
        } else {
          setState({ phase: 'done', data: result as PrDetail })
        }
      })
      .catch(err => {
        if (cancelled) return
        setState({ phase: 'error', message: String(err) })
      })

    return () => { cancelled = true }
  }, [prNumber])

  if (state.phase === 'loading') {
    return (
      <div data-testid="pr-detail-page" style={pageStyle}>
        <p style={{ color: '#9ca3af', fontSize: 14 }}>Loading PR #{prNumber}…</p>
      </div>
    )
  }

  if (state.phase === 'not_found') {
    return (
      <div data-testid="pr-detail-page" style={pageStyle}>
        <p style={{ color: '#f87171', fontSize: 18, margin: '0 0 12px' }}>
          PR #{prNumber} not found
        </p>
        <Link to="/prs" style={{ color: '#818cf8', fontSize: 14 }}>
          ← Back to PRs
        </Link>
      </div>
    )
  }

  if (state.phase === 'error') {
    return (
      <div data-testid="pr-detail-page" style={pageStyle}>
        <p style={{ color: '#f87171', fontSize: 14, margin: '0 0 12px' }}>
          Error: {state.message}
        </p>
        <Link to="/prs" style={{ color: '#818cf8', fontSize: 14 }}>
          ← Back to PRs
        </Link>
      </div>
    )
  }

  const { data } = state

  return (
    <div data-testid="pr-detail-page" style={pageStyle}>
      <header style={{ marginBottom: 24 }}>
        <Link to="/prs" style={{ color: '#6b7280', fontSize: 13, textDecoration: 'none' }}>
          ← Back to PRs
        </Link>
        <h1 style={{ margin: '8px 0 0', fontSize: 22, fontWeight: 700, color: '#f9fafb' }}>
          PR #{prNumber}
        </h1>
        {data.review_rounds > 0 && (
          <p style={{ margin: '4px 0 0', fontSize: 13, color: '#9ca3af' }}>
            {data.review_rounds} review round{data.review_rounds !== 1 ? 's' : ''}
          </p>
        )}
      </header>

      <PrMetaCard pr={data.pr} />
      <DiscussionLinkCard discussion={data.discussion} />
      <QualityScoreCard quality={data.quality} />
      <CostBreakdownCard cost={data.cost} />
    </div>
  )
}

const pageStyle: React.CSSProperties = {
  background: '#0f172a',
  minHeight: '100vh',
  padding: '32px 24px',
  fontFamily: 'system-ui, sans-serif',
  color: '#f9fafb',
  maxWidth: 900,
  boxSizing: 'border-box',
}
