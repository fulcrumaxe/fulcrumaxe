/**
 * OnboardingTour — first-run overlay tour for new operators.
 *
 * Fires on first load when localStorage.af_tour_seen is not set.
 * Walks through 5-6 steps across the 4 primary pages, highlighting
 * each target element with a spotlight cutout.
 *
 * Guard: returns null during automated tests (import.meta.env.MODE === 'test')
 * to avoid DOM-query failures in jsdom.
 */

import { useState, useEffect, useCallback, useRef } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import { useTourSteps } from '../hooks/useTourSteps'
import type { TourStep } from '../hooks/useTourSteps'

const TOUR_SEEN_KEY = 'af_tour_seen'
const ROUTE_SETTLE_MS = 150

interface SpotlightBox {
  top: number
  left: number
  width: number
  height: number
}

function getSpotlight(selector: string): SpotlightBox | null {
  const el = document.querySelector(selector)
  if (!el) return null
  const rect = el.getBoundingClientRect()
  const padding = 8
  return {
    top: rect.top - padding,
    left: rect.left - padding,
    width: rect.width + padding * 2,
    height: rect.height + padding * 2,
  }
}

function computeCalloutPosition(
  box: SpotlightBox,
  placement: TourStep['placement']
): React.CSSProperties {
  const calloutW = 300
  const gap = 16

  switch (placement) {
    case 'bottom':
      return {
        position: 'fixed',
        top: box.top + box.height + gap,
        left: box.left + box.width / 2 - calloutW / 2,
        width: calloutW,
      }
    case 'top':
      return {
        position: 'fixed',
        top: box.top - gap - 140, // approximate callout height
        left: box.left + box.width / 2 - calloutW / 2,
        width: calloutW,
      }
    case 'left':
      return {
        position: 'fixed',
        top: box.top + box.height / 2 - 70,
        left: box.left - calloutW - gap,
        width: calloutW,
      }
    case 'right':
      return {
        position: 'fixed',
        top: box.top + box.height / 2 - 70,
        left: box.left + box.width + gap,
        width: calloutW,
      }
  }
}

export default function OnboardingTour() {
  const steps = useTourSteps()
  const navigate = useNavigate()
  const location = useLocation()
  const [active, setActive] = useState(false)
  const [currentIndex, setCurrentIndex] = useState(0)
  const [spotlight, setSpotlight] = useState<SpotlightBox | null>(null)
  const settleTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // Track whether the step change was driven by a user action (Next/Back)
  // vs the initial tour start. On initial start we only navigate when the
  // user is already on the step's home route; otherwise we just show the
  // overlay without yanking them away from their current page.
  const userAdvancedRef = useRef(false)

  // Check if tour should start on mount.
  // Guard: skip in test environment to avoid jsdom DOM-query failures.
  useEffect(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    if ((import.meta as any).env?.MODE === 'test') return
    const seen = localStorage.getItem(TOUR_SEEN_KEY)
    if (!seen) {
      setActive(true)
    }
  }, [])

  const finish = useCallback(() => {
    localStorage.setItem(TOUR_SEEN_KEY, '1')
    setActive(false)
  }, [])

  // Navigate to step route and resolve spotlight after settle.
  // Skip navigation when the tour first becomes active and the user is
  // already on a page that is NOT the step's route — this prevents the
  // tour from silently redirecting deep-link pages like /runs or /stats
  // back to / on first load.
  useEffect(() => {
    if (!active) return
    const step = steps[currentIndex]
    if (!step) return

    const shouldNavigate = userAdvancedRef.current || location.pathname === step.route
    // Reset the flag so subsequent Next/Back clicks always navigate.
    userAdvancedRef.current = false

    if (shouldNavigate) {
      navigate(step.route)
    }

    if (settleTimerRef.current) clearTimeout(settleTimerRef.current)
    settleTimerRef.current = setTimeout(() => {
      const box = getSpotlight(step.selector)
      setSpotlight(box)
    }, ROUTE_SETTLE_MS)

    return () => {
      if (settleTimerRef.current) clearTimeout(settleTimerRef.current)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, currentIndex, steps, navigate])

  // Keyboard navigation
  useEffect(() => {
    if (!active) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'ArrowRight' || e.key === 'ArrowLeft' || e.key === 'Escape') {
        e.preventDefault()
        if (e.key === 'Escape') {
          finish()
        } else if (e.key === 'ArrowRight') {
          if (currentIndex < steps.length - 1) {
            userAdvancedRef.current = true
            setCurrentIndex(i => i + 1)
          } else {
            finish()
          }
        } else if (e.key === 'ArrowLeft') {
          if (currentIndex > 0) {
            userAdvancedRef.current = true
            setCurrentIndex(i => i - 1)
          }
        }
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [active, currentIndex, steps.length, finish])

  if (!active) return null

  const step = steps[currentIndex]
  const isLast = currentIndex === steps.length - 1
  const calloutStyle = spotlight
    ? computeCalloutPosition(spotlight, step.placement)
    : { position: 'fixed' as const, top: '50%', left: '50%', transform: 'translate(-50%, -50%)', width: 300 }

  // Clamp callout so it stays on screen
  const clampedStyle: React.CSSProperties = {
    ...calloutStyle,
    maxWidth: 300,
    zIndex: 10001,
  }
  if (typeof clampedStyle.left === 'number') {
    clampedStyle.left = Math.max(16, Math.min(clampedStyle.left as number, window.innerWidth - 320))
  }
  if (typeof clampedStyle.top === 'number') {
    clampedStyle.top = Math.max(16, Math.min(clampedStyle.top as number, window.innerHeight - 200))
  }

  return (
    <>
      {/* Dark overlay */}
      <div
        style={{
          position: 'fixed',
          inset: 0,
          zIndex: 9999,
          pointerEvents: 'none',
        }}
        aria-hidden="true"
      >
        {/* If spotlight found, use SVG clip-path; otherwise full overlay */}
        {spotlight ? (
          <svg
            width="100%"
            height="100%"
            style={{ position: 'absolute', inset: 0 }}
          >
            <defs>
              <mask id="tour-mask">
                <rect width="100%" height="100%" fill="white" />
                <rect
                  x={spotlight.left}
                  y={spotlight.top}
                  width={spotlight.width}
                  height={spotlight.height}
                  rx={6}
                  fill="black"
                />
              </mask>
            </defs>
            <rect
              width="100%"
              height="100%"
              fill="rgba(0,0,0,0.6)"
              mask="url(#tour-mask)"
            />
            {/* Spotlight border highlight */}
            <rect
              x={spotlight.left}
              y={spotlight.top}
              width={spotlight.width}
              height={spotlight.height}
              rx={6}
              fill="none"
              stroke="#f59e0b"
              strokeWidth={2}
            />
          </svg>
        ) : (
          <div
            style={{
              position: 'absolute',
              inset: 0,
              background: 'rgba(0,0,0,0.6)',
            }}
          />
        )}
      </div>

      {/* Click-blocker overlay so user can't click behind the tour */}
      <div
        style={{
          position: 'fixed',
          inset: 0,
          zIndex: 10000,
          cursor: 'default',
        }}
        onClick={e => e.stopPropagation()}
      />

      {/* Callout card */}
      <div
        role="dialog"
        aria-modal="true"
        aria-label={`Onboarding tour step ${currentIndex + 1} of ${steps.length}`}
        style={{
          ...clampedStyle,
          background: '#1f2937',
          border: '1px solid #374151',
          borderRadius: 10,
          padding: 20,
          boxShadow: '0 8px 32px rgba(0,0,0,0.5)',
          color: '#f9fafb',
          fontFamily: 'system-ui, sans-serif',
        }}
      >
        {/* Step counter + Skip */}
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            marginBottom: 10,
          }}
        >
          <span style={{ fontSize: 11, color: '#6b7280' }}>
            {currentIndex + 1} / {steps.length}
          </span>
          <button
            type="button"
            onClick={finish}
            style={{
              background: 'transparent',
              border: 'none',
              color: '#6b7280',
              cursor: 'pointer',
              fontSize: 18,
              lineHeight: 1,
              padding: '2px 4px',
            }}
            aria-label="Skip tour"
          >
            ×
          </button>
        </div>

        <h3 style={{ margin: '0 0 8px', fontSize: 15, fontWeight: 600, color: '#f9fafb' }}>
          {step.title}
        </h3>
        <p style={{ margin: '0 0 16px', fontSize: 13, color: '#d1d5db', lineHeight: 1.5 }}>
          {step.body}
        </p>

        {/* Navigation */}
        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          {currentIndex > 0 && (
            <button
              type="button"
              onClick={() => { userAdvancedRef.current = true; setCurrentIndex(i => i - 1) }}
              style={{
                background: 'transparent',
                border: '1px solid #374151',
                color: '#9ca3af',
                borderRadius: 6,
                padding: '6px 14px',
                cursor: 'pointer',
                fontSize: 13,
              }}
            >
              Back
            </button>
          )}
          <button
            type="button"
            onClick={() => {
              if (isLast) {
                finish()
              } else {
                userAdvancedRef.current = true
                setCurrentIndex(i => i + 1)
              }
            }}
            style={{
              background: '#2563eb',
              border: 'none',
              color: '#fff',
              borderRadius: 6,
              padding: '6px 16px',
              cursor: 'pointer',
              fontSize: 13,
              fontWeight: 500,
            }}
          >
            {isLast ? 'Done' : 'Next'}
          </button>
        </div>
      </div>
    </>
  )
}
