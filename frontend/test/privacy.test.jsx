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
