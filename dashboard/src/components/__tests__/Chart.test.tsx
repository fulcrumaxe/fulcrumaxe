import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Chart } from '../Chart'

describe('Chart', () => {
  const data = [
    { x: 'Jan', y: 10 },
    { x: 'Feb', y: 20 },
    { x: 'Mar', y: 15 },
  ]

  it('renders an SVG element for line chart', () => {
    const { container } = render(<Chart data={data} type="line" />)
    expect(container.querySelector('svg')).not.toBeNull()
  })

  it('renders an SVG element for bar chart', () => {
    const { container } = render(<Chart data={data} type="bar" />)
    expect(container.querySelector('svg')).not.toBeNull()
  })

  it('renders data points as circles for line chart', () => {
    const { container } = render(<Chart data={data} type="line" />)
    const circles = container.querySelectorAll('circle')
    expect(circles.length).toBe(data.length)
  })

  it('renders bars as rects for bar chart', () => {
    const { container } = render(<Chart data={data} type="bar" />)
    const rects = container.querySelectorAll('rect')
    expect(rects.length).toBe(data.length)
  })

  it('renders empty state when no data', () => {
    render(<Chart data={[]} />)
    expect(screen.getByText('No data')).toBeInTheDocument()
  })

  it('sets aria-label from label prop', () => {
    render(<Chart data={data} label="Velocity chart" />)
    expect(screen.getByRole('img', { name: 'Velocity chart' })).toBeInTheDocument()
  })

  it('uses default aria-label when no label provided', () => {
    render(<Chart data={data} type="line" />)
    expect(screen.getByRole('img', { name: 'Line chart' })).toBeInTheDocument()
  })
})
