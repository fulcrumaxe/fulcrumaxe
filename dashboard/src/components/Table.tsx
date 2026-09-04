import { useState } from 'react'

export interface Column<T> {
  key: keyof T
  header: string
  render?: (value: T[keyof T], row: T) => React.ReactNode
}

interface Props<T> {
  columns: Column<T>[]
  rows: T[]
  keyField: keyof T
}

export function Table<T>({ columns, rows, keyField }: Props<T>) {
  const [sortKey, setSortKey] = useState<keyof T | null>(null)
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('asc')

  const handleSort = (key: keyof T) => {
    if (sortKey === key) {
      setSortDir(d => (d === 'asc' ? 'desc' : 'asc'))
    } else {
      setSortKey(key)
      setSortDir('asc')
    }
  }

  const sorted = sortKey
    ? [...rows].sort((a, b) => {
        const av = a[sortKey]
        const bv = b[sortKey]
        if (av === bv) return 0
        const cmp = av < bv ? -1 : 1
        return sortDir === 'asc' ? cmp : -cmp
      })
    : rows

  return (
    <div className="table-wrapper">
      <table className="table">
        <thead>
          <tr>
            {columns.map(col => (
              <th
                key={String(col.key)}
                onClick={() => handleSort(col.key)}
                className={`table-th ${sortKey === col.key ? `table-th--sorted-${sortDir}` : ''}`}
                aria-sort={
                  sortKey === col.key ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'
                }
              >
                {col.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map(row => (
            <tr key={String(row[keyField])} className="table-row">
              {columns.map(col => (
                <td key={String(col.key)} className="table-td">
                  {col.render ? col.render(row[col.key], row) : String(row[col.key] ?? '')}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
