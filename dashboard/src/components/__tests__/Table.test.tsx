import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { Table, type Column } from '../Table'

interface Row {
  id: number
  name: string
  value: number
}

const columns: Column<Row>[] = [
  { key: 'id', header: 'ID' },
  { key: 'name', header: 'Name' },
  { key: 'value', header: 'Value' },
]

const rows: Row[] = [
  { id: 1, name: 'Alpha', value: 30 },
  { id: 2, name: 'Beta', value: 10 },
  { id: 3, name: 'Gamma', value: 20 },
]

describe('Table', () => {
  it('renders column headers', () => {
    render(<Table columns={columns} rows={rows} keyField="id" />)
    expect(screen.getByText('ID')).toBeInTheDocument()
    expect(screen.getByText('Name')).toBeInTheDocument()
    expect(screen.getByText('Value')).toBeInTheDocument()
  })

  it('renders all rows', () => {
    render(<Table columns={columns} rows={rows} keyField="id" />)
    expect(screen.getByText('Alpha')).toBeInTheDocument()
    expect(screen.getByText('Beta')).toBeInTheDocument()
    expect(screen.getByText('Gamma')).toBeInTheDocument()
  })

  it('sorts ascending on column click', () => {
    render(<Table columns={columns} rows={rows} keyField="id" />)
    fireEvent.click(screen.getByText('Name'))
    const cells = screen.getAllByRole('cell')
    const names = cells.filter(c => ['Alpha', 'Beta', 'Gamma'].includes(c.textContent ?? ''))
    expect(names[0].textContent).toBe('Alpha')
    expect(names[1].textContent).toBe('Beta')
    expect(names[2].textContent).toBe('Gamma')
  })

  it('sorts descending on second click', () => {
    render(<Table columns={columns} rows={rows} keyField="id" />)
    fireEvent.click(screen.getByText('Value'))
    fireEvent.click(screen.getByText('Value'))
    const cells = screen.getAllByRole('cell')
    const values = cells.filter(c => ['10', '20', '30'].includes(c.textContent ?? ''))
    expect(values[0].textContent).toBe('30')
    expect(values[1].textContent).toBe('20')
    expect(values[2].textContent).toBe('10')
  })

  it('uses custom render function when provided', () => {
    const cols: Column<Row>[] = [
      { key: 'id', header: 'ID' },
      { key: 'name', header: 'Name', render: v => <strong>{String(v)}</strong> },
    ]
    render(<Table columns={cols} rows={[rows[0]]} keyField="id" />)
    expect(screen.getByText('Alpha').tagName).toBe('STRONG')
  })
})
