import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { StatusBadge } from '../StatusBadge'

describe('StatusBadge', () => {
  it('renders the label text', () => {
    render(<StatusBadge status="success" label="healthy" />)
    expect(screen.getByText('healthy')).toBeInTheDocument()
  })

  it('applies success class for success status', () => {
    const { container } = render(<StatusBadge status="success" label="ok" />)
    expect(container.querySelector('.status-badge--success')).not.toBeNull()
  })

  it('applies warning class for warning status', () => {
    const { container } = render(<StatusBadge status="warning" label="degraded" />)
    expect(container.querySelector('.status-badge--warning')).not.toBeNull()
  })

  it('applies error class for error status', () => {
    const { container } = render(<StatusBadge status="error" label="critical" />)
    expect(container.querySelector('.status-badge--error')).not.toBeNull()
  })

  it('applies neutral class for neutral status', () => {
    const { container } = render(<StatusBadge status="neutral" label="idle" />)
    expect(container.querySelector('.status-badge--neutral')).not.toBeNull()
  })

  it('applies info class for info status', () => {
    const { container } = render(<StatusBadge status="info" label="running" />)
    expect(container.querySelector('.status-badge--info')).not.toBeNull()
  })

  it('has role=status for accessibility', () => {
    render(<StatusBadge status="success" label="ok" />)
    expect(screen.getByRole('status')).toBeInTheDocument()
  })
})
