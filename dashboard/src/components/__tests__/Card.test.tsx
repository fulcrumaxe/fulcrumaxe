import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Card } from '../Card'

describe('Card', () => {
  it('renders the title', () => {
    render(<Card title="Test Title"><p>Content</p></Card>)
    expect(screen.getByText('Test Title')).toBeInTheDocument()
  })

  it('renders children', () => {
    render(<Card title="Test"><p>Child content</p></Card>)
    expect(screen.getByText('Child content')).toBeInTheDocument()
  })

  it('renders subtitle when provided', () => {
    render(<Card title="T" subtitle="Sub">x</Card>)
    expect(screen.getByText('Sub')).toBeInTheDocument()
  })

  it('does not render subtitle when omitted', () => {
    render(<Card title="T">x</Card>)
    expect(screen.queryByText('Sub')).not.toBeInTheDocument()
  })

  it('applies custom className', () => {
    const { container } = render(<Card title="T" className="my-class">x</Card>)
    expect(container.querySelector('.my-class')).not.toBeNull()
  })
})
