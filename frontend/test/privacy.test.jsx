import { render, screen } from '@testing-library/react'
import { describe, expect, test } from 'vitest'
import { MetadataTable } from '../src/components/MetadataTable'
import { PrivacySummary, SensitivityBadge, summarizeSensitivity } from '../src/components/PrivacyIndicators'

const f = (key, sensitivity, extra = {}) => ({ key, label: key.split(':').pop(), value: 'x', raw_value: 'x',
  editable: false, deletable: false, field_type: 'str', source: key.split(':')[0], choices: [], multi: false,
  length: null, binary: null, sensitivity, ...extra })

const fields = [
  f('gps:Latitude', 'location'), f('gps:Longitude', 'location'), f('exif:Make', 'device'),
  f('exif:DateTime', 'datetime'), f('exif:Orientation', null),
]

describe('privacy indicators', () => {
  test('categories are counted and ordered by seriousness', () => {
    expect(summarizeSensitivity(fields)).toEqual([
      { category: 'location', count: 2 }, { category: 'device', count: 1 }, { category: 'datetime', count: 1 }])
    expect(summarizeSensitivity([f('img:width', null)])).toEqual([])
  })

  test('the summary names what sharing reveals', () => {
    render(<PrivacySummary fields={fields} />)
    const note = screen.getByRole('note', { name: 'Privacy summary' })
    expect(note.textContent).toContain('Sharing this file reveals')
    expect(note.textContent).toContain('Location · 2')
    expect(note.textContent).toContain('Device · 1')
  })

  test('no summary for a file without sensitive fields', () => {
    const { container } = render(<PrivacySummary fields={[f('img:width', null)]} />)
    expect(container.innerHTML).toBe('')
  })

  test('each sensitive field carries its badge, technical ones do not', () => {
    const { rerender } = render(<SensitivityBadge field={f('gps:Latitude', 'location')} />)
    expect(screen.getByText('Location')).toBeTruthy()
    rerender(<SensitivityBadge field={f('img:width', null)} />)
    expect(screen.queryByText('Location')).toBeNull()
  })

  test('the table shows the summary and the badges', () => {
    render(<MetadataTable fields={fields} />)
    expect(screen.getByRole('note', { name: 'Privacy summary' })).toBeTruthy()
    expect(screen.getAllByText('Location').length).toBeGreaterThanOrEqual(2)
  })
})

describe('search', () => {
  test('finds a field by its key, its label, its value or another name', async () => {
    const { fireEvent } = await import('@testing-library/react')
    const list = [
      { ...f('exif:DateTimeOriginal', 'datetime'), value: '2026-01-02T03:04:05', label: 'DateTimeOriginal' },
      { ...f('exif:Make', 'device'), value: 'Canon' },
      { ...f('exif:ISOSpeedRatings', null), value: 200, label: 'ISOSpeedRatings' },
    ]
    render(<MetadataTable fields={list} />)
    const search = screen.getByLabelText('Search fields')
    fireEvent.change(search, { target: { value: 'date taken' } })          // another name
    expect(screen.queryByText('DateTimeOriginal')).toBeTruthy()
    expect(screen.queryByText('Make')).toBeNull()
    fireEvent.change(search, { target: { value: 'canon' } })               // a value
    expect(screen.queryByText('Make')).toBeTruthy()
    fireEvent.change(search, { target: { value: 'iso' } })
    expect(screen.queryByText('ISOSpeedRatings')).toBeTruthy()
    fireEvent.change(search, { target: { value: 'zzz' } })
    expect(screen.getByText('No field matches')).toBeTruthy()
  })
})
