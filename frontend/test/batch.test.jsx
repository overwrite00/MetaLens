import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, test, vi } from 'vitest'
import { metalens } from '../src/api/client'
import { BatchDialog } from '../src/components/BatchDialog'

vi.mock('../src/api/client', () => ({
  metalens: { sanitizeMany: vi.fn(), shiftDates: vi.fn(), copyTags: vi.fn(), rename: vi.fn(), exportFolder: vi.fn() },
}))

const paths = ['C:/p/a.jpg', 'C:/p/b.jpg']
const previewBody = { ok: true, results: [
  { path: paths[0], ok: true, will_remove: [{ key: 'gps:Latitude' }], remaining: [], undo_token: null },
  { path: paths[1], ok: false, status: 403, errors: [{ code: 'not_writable', key: null, message: 'file is read-only' }] },
], undo_tokens: [] }

beforeEach(() => { vi.resetAllMocks() })

describe('Batch dialog', () => {
  test('previews every file first and shows a failing file without stopping', async () => {
    metalens.sanitizeMany.mockResolvedValue(previewBody)
    render(<BatchDialog paths={paths} folder="C:/p" onClose={() => {}} />)
    expect(screen.getByRole('button', { name: /Apply to/ }).disabled).toBe(true)         // nothing is applied unseen
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }))
    const results = await screen.findByLabelText('Results')
    expect(within(results).getByText('a.jpg')).toBeTruthy()
    expect(results.textContent).toContain('1 to remove')
    expect(results.textContent).toContain('not done: file is read-only')
    expect(metalens.sanitizeMany.mock.calls[0][2]).toEqual({ preview: true })
  })

  test('applying pushes one undo entry per file', async () => {
    const onUndoPush = vi.fn()
    metalens.sanitizeMany.mockImplementation(async (_p, _c, { preview }) => preview ? previewBody
      : { ok: true, results: [{ path: paths[0], ok: true, removed: [{ key: 'x' }], remaining: [], undo_token: 'tok-a' }], undo_tokens: ['tok-a'] })
    render(<BatchDialog paths={paths} folder="C:/p" onClose={() => {}} onUndoPush={onUndoPush} />)
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }))
    await screen.findByLabelText('Results')
    fireEvent.click(screen.getByRole('button', { name: /Apply to 2 file/ }))
    await screen.findByRole('status')
    expect(onUndoPush).toHaveBeenCalledWith({ filePath: paths[0], token: 'tok-a', label: 'Prepare for sharing' })
  })

  test('dates are shifted by the chosen amount of the chosen unit', async () => {
    metalens.shiftDates.mockResolvedValue({ ok: true, results: [{ path: paths[0], ok: true, changes: [{ key: 'k' }], skipped: [] }], undo_tokens: [] })
    render(<BatchDialog paths={paths} folder="C:/p" onClose={() => {}} />)
    fireEvent.click(screen.getByRole('tab', { name: /Shift dates/ }))
    fireEvent.change(screen.getByLabelText('Amount'), { target: { value: '-2' } })
    fireEvent.change(screen.getByLabelText('Unit'), { target: { value: 'days' } })
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }))
    await waitFor(() => expect(metalens.shiftDates).toHaveBeenCalled())
    expect(metalens.shiftDates.mock.calls[0][1]).toBe(-2 * 86400)
  })

  test('copying needs at least two files and uses the first as the source', async () => {
    metalens.copyTags.mockResolvedValue({ ok: true, results: [{ path: paths[1], ok: true, will_copy: ['exif:Artist'], skipped: [] }], undo_tokens: [] })
    const { rerender } = render(<BatchDialog paths={[paths[0]]} folder="C:/p" onClose={() => {}} />)
    fireEvent.click(screen.getByRole('tab', { name: /Copy tags/ }))
    expect(screen.getByRole('button', { name: 'Preview' }).disabled).toBe(true)
    rerender(<BatchDialog paths={paths} folder="C:/p" onClose={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }))
    await waitFor(() => expect(metalens.copyTags).toHaveBeenCalledWith(paths[0], [paths[1]], { preview: true }))
  })

  test('export saves the folder metadata with the chosen format', async () => {
    metalens.exportFolder.mockResolvedValue('file,handler\n')
    const click = vi.fn()
    URL.createObjectURL = vi.fn(() => 'blob:x')
    URL.revokeObjectURL = vi.fn()
    const create = document.createElement.bind(document)
    vi.spyOn(document, 'createElement').mockImplementation(tag => {
      const element = create(tag)
      if (tag === 'a') element.click = click
      return element
    })
    render(<BatchDialog paths={[]} folder="C:/p" onClose={() => {}} />)
    fireEvent.click(screen.getByRole('tab', { name: /Export/ }))
    fireEvent.click(screen.getByRole('button', { name: /Save the metadata/ }))
    await waitFor(() => expect(click).toHaveBeenCalled())
    expect(metalens.exportFolder).toHaveBeenCalledWith('C:/p', 'csv')
  })
})

describe('Add location', () => {
  test('fixed coordinates are sent as numbers, a GPX track with the clock offset', async () => {
    metalens.geotag = vi.fn().mockResolvedValue({ ok: true, results: [{ path: paths[0], ok: true, will_write: ['gps:Latitude'], position: [1, 2, null] }], undo_tokens: [] })
    render(<BatchDialog paths={paths} folder="C:/p" onClose={() => {}} />)
    fireEvent.click(screen.getByRole('tab', { name: /Add location/ }))
    expect(screen.getByRole('button', { name: 'Preview' }).disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('Latitude'), { target: { value: '45.5' } })
    fireEvent.change(screen.getByLabelText('Longitude'), { target: { value: '9.25' } })
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }))
    await waitFor(() => expect(metalens.geotag).toHaveBeenCalledWith(paths, { latitude: 45.5, longitude: 9.25 }, { preview: true }))
    fireEvent.change(screen.getByLabelText('GPX file'), { target: { value: 'C:/t/track.gpx' } })
    fireEvent.change(screen.getByLabelText('Clock offset'), { target: { value: '2' } })
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }))
    await waitFor(() => expect(metalens.geotag).toHaveBeenLastCalledWith(
      paths, { gpx: 'C:/t/track.gpx', utc_offset_minutes: 120 }, { preview: true }))
  })
})
