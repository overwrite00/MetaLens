import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, test, vi } from 'vitest'
import { metalens } from '../src/api/client'
import { PRESETS, SharePanel } from '../src/components/SharePanel'

vi.mock('../src/api/client', () => ({ metalens: { sanitize: vi.fn() } }))

const field = (key, sensitivity) => ({ key, label: key.split(':').pop(), value: 'x', sensitivity })
const record = { fields: [field('gps:Latitude', 'location'), field('exif:Make', 'device'), field('img:width', null)] }
const previewBody = { ok: true, preview: true, undo_token: null, warnings: [], remaining: [],
  will_remove: [{ key: 'gps:Latitude', label: 'Latitude', category: 'location' }] }

beforeEach(() => {
  vi.resetAllMocks()
  metalens.sanitize.mockResolvedValue(previewBody)
})

describe('Prepare for sharing', () => {
  test('previews first, with the personal-data preset selected', async () => {
    render(<SharePanel record={record} filePath="a.jpg" />)
    await screen.findByLabelText('Preview')
    const [, categories, options] = metalens.sanitize.mock.calls[0]
    expect(categories).toEqual([...PRESETS[1].categories].sort())
    expect(options).toEqual({ preview: true })
    expect(screen.getByText('Latitude')).toBeTruthy()
  })

  test('presets change the categories and the preview is requested again', async () => {
    render(<SharePanel record={record} filePath="a.jpg" />)
    await screen.findByLabelText('Preview')
    fireEvent.click(screen.getByRole('button', { name: 'Remove location' }))
    await waitFor(() => expect(metalens.sanitize.mock.calls.at(-1)[1]).toEqual(['location']))
  })

  test('applying removes, pushes an undo entry and reports the result', async () => {
    const onUndoPush = vi.fn()
    const onSaved = vi.fn()
    metalens.sanitize.mockImplementation(async (_path, _cats, { preview }) => preview
      ? previewBody
      : { ...previewBody, preview: false, undo_token: 'tok', removed: previewBody.will_remove, remaining: [] })
    render(<SharePanel record={record} filePath="a.jpg" onUndoPush={onUndoPush} onSaved={onSaved} />)
    await screen.findByLabelText('Preview')
    fireEvent.click(screen.getByRole('button', { name: /Remove 1 field/ }))
    await screen.findByRole('status')
    expect(onUndoPush).toHaveBeenCalledWith({ filePath: 'a.jpg', token: 'tok', label: 'Prepare for sharing' })
    expect(onSaved).toHaveBeenCalled()
    expect(screen.getByRole('status').textContent).toContain('Removed 1 field')
  })

  test('what cannot be removed is listed with its reason', async () => {
    metalens.sanitize.mockResolvedValue({ ...previewBody, will_remove: [],
      remaining: [{ key: 'gps:Latitude', label: 'Latitude', category: 'location', reason: 'read-only format' }] })
    render(<SharePanel record={record} filePath="a.heic" />)
    const preview = await screen.findByLabelText('Preview')
    expect(preview.textContent).toContain('cannot be removed from this file')
    expect(preview.textContent).toContain('read-only format')
    expect(screen.getByRole('button', { name: 'Remove' }).disabled).toBe(true)
  })

  test('a server error is shown', async () => {
    metalens.sanitize.mockRejectedValue(new Error('boom'))
    render(<SharePanel record={record} filePath="a.jpg" />)
    expect((await screen.findByRole('alert')).textContent).toContain('boom')
  })
})
