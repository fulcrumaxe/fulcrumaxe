/**
 * Tooltip — lightweight hover/focus tooltip with no external dependencies.
 *
 * Usage:
 *   <Tooltip label="What this button does">
 *     <button>Click me</button>
 *   </Tooltip>
 *
 * Accessibility: child gets aria-describedby pointing at the tooltip element
 * (role="tooltip"). Shows after 300ms hover/focus, hides on blur/mouseleave/Esc.
 */

import {
  useState,
  useRef,
  useCallback,
  useId,
  type ReactElement,
  cloneElement,
} from 'react'

interface TooltipProps {
  label: string
  placement?: 'top' | 'bottom' | 'left' | 'right'
  children: ReactElement
}

const SHOW_DELAY_MS = 300

export function Tooltip({ label, placement = 'top', children }: TooltipProps) {
  const [visible, setVisible] = useState(false)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const tooltipId = useId()

  const show = useCallback(() => {
    timerRef.current = setTimeout(() => setVisible(true), SHOW_DELAY_MS)
  }, [])

  const hide = useCallback(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current)
      timerRef.current = null
    }
    setVisible(false)
  }, [])

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Escape') hide()
    },
    [hide]
  )

  // Offset from the child element edge
  const offset = 8

  const positionStyle: React.CSSProperties = (() => {
    switch (placement) {
      case 'bottom':
        return { top: '100%', left: '50%', transform: `translate(-50%, ${offset}px)` }
      case 'left':
        return { top: '50%', right: '100%', transform: `translate(-${offset}px, -50%)` }
      case 'right':
        return { top: '50%', left: '100%', transform: `translate(${offset}px, -50%)` }
      case 'top':
      default:
        return { bottom: '100%', left: '50%', transform: `translate(-50%, -${offset}px)` }
    }
  })()

  const tooltipStyle: React.CSSProperties = {
    position: 'absolute',
    ...positionStyle,
    zIndex: 9000,
    background: '#374151',
    color: '#f9fafb',
    padding: '5px 10px',
    borderRadius: 5,
    fontSize: 12,
    lineHeight: 1.4,
    whiteSpace: 'normal',
    maxWidth: 260,
    boxShadow: '0 2px 8px rgba(0,0,0,0.4)',
    border: '1px solid #4b5563',
    pointerEvents: 'none',
  }

  return (
    <span
      style={{ position: 'relative', display: 'inline-block' }}
      onMouseEnter={show}
      onMouseLeave={hide}
      onFocus={show}
      onBlur={hide}
      onKeyDown={handleKeyDown}
    >
      {cloneElement(children, { 'aria-describedby': tooltipId })}
      {visible && (
        <span
          id={tooltipId}
          role="tooltip"
          style={tooltipStyle}
        >
          {label}
        </span>
      )}
    </span>
  )
}
