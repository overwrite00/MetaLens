import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, test, vi } from 'vitest'
import { MetadataEditor } from '../src/components/MetadataEditor'
import { metalens } from '../src/api/client'
import { fromInput, toInput } from '../src/lib/serializeValue'

vi.mock('../src/api/client', () => ({ metalens: { write: vi.fn(), delete: vi.fn() } }))

const f = (key, value, extra = {}) => ({ key, label: extra.label ?? key, value, raw_value: value,
  editable: true, deletable: true, field_type: 'str', source: 'x', choices: [], multi: false,
  length: null, binary: null, ...extra })
const rec = (fields, extra = {}) => ({ supports_write: true, supports_delete: true, format_supports_write: true,
  format_supports_delete: true, read_only_reason: null, warnings: [], read_errors: [], addable_fields: [], fields, ...extra })

function editor(record, path = 'a.file') {
  const props = { record, filePath: path, onSaved: vi.fn(), onUndoPush: vi.fn() }
  return { ...render(<MetadataEditor {...props} />), props }
}
async function save() {
  fireEvent.click(screen.getByText('Save'))
  await waitFor(() => expect(metalens.write).toHaveBeenCalled())
  return metalens.write.mock.calls[0]
}
const row = key => document.querySelector(`[data-field-key="${key}"]`)

beforeEach(() => { vi.resetAllMocks(); metalens.write.mockResolvedValue({ ok: true }); metalens.delete.mockResolvedValue({ ok: true }) })

describe('typed values are sent with their JSON type', () => {
  test('lists are sent as arrays, never joined', async () => {
    editor(rec([f('tag:TPE1', ['Alice', 'Bob'], { multi: true, label: 'Artist' })]))
    const items = within(row('tag:TPE1')).getAllByRole('textbox')
    expect(items.map(i => i.value)).toEqual(['Alice', 'Bob'])
    fireEvent.change(items[1], { target: { value: 'Carol' } })
    fireEvent.click(within(row('tag:TPE1')).getByRole('button', { name: 'Add Artist value' }))
    fireEvent.change(within(row('tag:TPE1')).getAllByRole('textbox')[2], { target: { value: 'Dan' } })
    fireEvent.click(within(row('tag:TPE1')).getByRole('button', { name: 'Remove Artist value 1' }))
    const [path, fields] = await save()
    expect(path).toBe('a.file')
    expect(fields).toEqual([{ key: 'tag:TPE1', value: ['Carol', 'Dan'] }])
  })

  test('fixed-length tuples keep their length and item type', async () => {
    editor(rec([f('mp4:trkn', [3, 10], { field_type: 'int', multi: true, length: 2, label: 'Track' })]))
    const items = within(row('mp4:trkn')).getAllByRole('spinbutton')
    expect(items).toHaveLength(2)
    expect(within(row('mp4:trkn')).queryByRole('button', { name: /Add Track/ })).toBeNull()
    fireEvent.change(items[0], { target: { value: '4' } })
    const [, fields] = await save()
    expect(fields).toEqual([{ key: 'mp4:trkn', value: [4, 10] }])
  })

  test('numbers are numbers and booleans are booleans', async () => {
    editor(rec([
      f('tag:TBPM', 120, { field_type: 'int', label: 'BPM' }),
      f('xmp:Rating', 2.5, { field_type: 'float', label: 'Rating' }),
      f('mp4:cpil', false, { field_type: 'bool', label: 'Compilation' }),
    ]))
    fireEvent.change(screen.getByLabelText('BPM'), { target: { value: '121' } })
    fireEvent.change(screen.getByLabelText('Rating'), { target: { value: '4.75' } })
    fireEvent.click(screen.getByLabelText('Compilation'))
    const [, fields] = await save()
    expect(fields).toEqual([
      { key: 'tag:TBPM', value: 121 },
      { key: 'xmp:Rating', value: 4.75 },
      { key: 'mp4:cpil', value: true },
    ])
  })

  test('date-times keep an explicit offset; local date-times have none', async () => {
    editor(rec([
      f('pdf:ModDate', '2026-01-02T03:04:05+02:00', { field_type: 'datetime', label: 'Modified' }),
      f('exif:DateTimeOriginal', '2026-01-02T03:04:05', { field_type: 'localdatetime', label: 'Taken' }),
    ]))
    expect(screen.getByLabelText('Modified UTC offset').value).toBe('+02:00')
    fireEvent.change(screen.getByLabelText('Modified'), { target: { value: '2026-03-04T05:06:07' } })
    fireEvent.change(screen.getByLabelText('Modified UTC offset'), { target: { value: '-05:30' } })
    fireEvent.change(screen.getByLabelText('Taken'), { target: { value: '2027-01-01T10:00' } })
    const [, fields] = await save()
    expect(fields).toEqual([
      { key: 'pdf:ModDate', value: '2026-03-04T05:06:07-05:30' },
      { key: 'exif:DateTimeOriginal', value: '2027-01-01T10:00:00' },
    ])
  })

  test('enum, text and rational use dedicated inputs', async () => {
    editor(rec([
      f('exif:Orientation', 1, { field_type: 'enum', choices: [1, 3, 6, 8], label: 'Orientation' }),
      f('tag:USLT', 'line 1\nline 2', { field_type: 'text', label: 'Lyrics' }),
      f('exif:ExposureTime', '1/125', { field_type: 'rational', label: 'Exposure' }),
    ]))
    const select = screen.getByLabelText('Orientation')
    expect(select.tagName).toBe('SELECT')
    fireEvent.change(select, { target: { value: '2' } })
    const lyrics = screen.getByLabelText('Lyrics')
    expect(lyrics.tagName).toBe('TEXTAREA')
    fireEvent.change(lyrics, { target: { value: 'a\nb' } })
    fireEvent.change(screen.getByLabelText('Exposure'), { target: { value: ' 1 / 250 ' } })
    const [, fields] = await save()
    expect(fields).toEqual([
      { key: 'exif:Orientation', value: 6 },
      { key: 'tag:USLT', value: 'a\nb' },
      { key: 'exif:ExposureTime', value: '1/250' },
    ])
  })

  test('invalid values are reported per field and nothing is sent', async () => {
    editor(rec([
      f('exif:ExposureTime', '1/125', { field_type: 'rational', label: 'Exposure' }),
      f('tag:TBPM', 120, { field_type: 'int', label: 'BPM' }),
    ]))
    fireEvent.change(screen.getByLabelText('Exposure'), { target: { value: 'fast' } })
    fireEvent.change(screen.getByLabelText('BPM'), { target: { value: '1.5' } })
    fireEvent.click(screen.getByText('Save'))
    expect(await screen.findByText(/2 invalid values/)).toBeTruthy()
    expect(within(row('exif:ExposureTime')).getByRole('alert').textContent).toMatch(/n\/d/)
    expect(within(row('tag:TBPM')).getByRole('alert').textContent).toMatch(/integer/)
    expect(metalens.write).not.toHaveBeenCalled()
  })

  test('server field errors are shown next to the field', async () => {
    const err = Object.assign(new Error('pdf:Title: too long'), {
      status: 422, errors: [{ code: 'too_large', key: 'pdf:Title', message: 'pdf:Title: too long' }] })
    metalens.write.mockRejectedValueOnce(err)
    editor(rec([f('pdf:Title', 'a', { label: 'Title' })]))
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'b' } })
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(within(row('pdf:Title')).getByRole('alert').textContent).toBe('pdf:Title: too long'))
  })
})

describe('serializeValue', () => {
  test('never stringifies arrays and round-trips canonical values', () => {
    const list = f('k', ['a', 'b'], { multi: true })
    expect(fromInput(list, toInput(list))).toEqual(['a', 'b'])
    const dt = f('k', '2026-01-02T03:04:05Z', { field_type: 'datetime' })
    expect(fromInput(dt, toInput(dt))).toBe('2026-01-02T03:04:05+00:00')
    const dur = f('k', 12.5, { field_type: 'duration' })
    expect(fromInput(dur, toInput(dur))).toBe(12.5)
    expect(() => fromInput(dur, '-1')).toThrow(/negative|>= 0/)
    expect(() => fromInput(f('k', 'x', { field_type: 'datetime' }), { local: '2026-01-02T03:04', offset: 'CET' })).toThrow(/offset/)
    expect(() => fromInput(f('k', 'x', { field_type: 'binary' }), null)).toThrow(/read-only/)
    expect(() => fromInput(list, [])).toThrow(/at least one/)
  })
})

describe('serializeValue: integers beyond 2^53', () => {
  const big = '1152921504606846979'
  test('are shown exactly and sent back as decimal strings (the server parses them exactly)', () => {
    const fld = f('mp4:plID', big, { field_type: 'int' })
    expect(toInput(fld)).toBe(big)
    expect(fromInput(fld, big)).toBe(big)
    expect(fromInput(fld, '42')).toBe(42)
    const list = f('k', [big, 7], { field_type: 'int', multi: true })
    expect(fromInput(list, toInput(list))).toEqual([big, 7])
  })
  test('still rejects non-integers and out-of-range 64-bit values', () => {
    const fld = f('k', 1, { field_type: 'int' })
    expect(() => fromInput(fld, '1.5')).toThrow(/integer/i)
    expect(() => fromInput(fld, '1e3')).toThrow(/integer/i)
    expect(() => fromInput(fld, '9'.repeat(30))).toThrow(/too large/i)
  })
})

describe('field visibility and permissions', () => {
  test('binary rows have no input, only delete', async () => {
    const descriptor = { size: 2048, sha256: 'c'.repeat(64), mime: 'image/png' }
    editor(rec([f('tag:APIC', descriptor, { field_type: 'binary', binary: descriptor, editable: false, label: 'Cover' })]))
    const r = row('tag:APIC')
    expect(r.querySelectorAll('input, textarea, select')).toHaveLength(0)
    expect(r.textContent).toContain('2048 bytes')
    expect(r.textContent).toContain('image/png')
    fireEvent.click(within(r).getByRole('button', { name: 'Delete Cover' }))
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(metalens.write).toHaveBeenCalledExactlyOnceWith('a.file', [], ['tag:APIC']))
    expect(metalens.delete).not.toHaveBeenCalled()
  })

  test('editability comes only from server flags, unknown prefixes are not hidden', () => {
    editor(rec([
      f('img:width', 100, { field_type: 'int', label: 'Width' }),             // was hidden by prefix
      f('audio:comment', 'x', { label: 'Audio comment' }),                    // was hidden by prefix
      f('xmp:dc:title', 'X', { label: 'XMP title' }),
      f('weird:thing', 'w', { label: 'Weird' }),
      f('noprefix', 'n', { label: 'No prefix' }),
      f('pdf:Producer', 'p', { editable: false, deletable: false, label: 'Producer' }),
      f('pdf:Keywords', 'k', { editable: false, deletable: true, label: 'Keywords' }),
    ]))
    for (const label of ['Width', 'Audio comment', 'XMP title', 'Weird', 'No prefix']) {
      expect(screen.getByLabelText(label)).toBeTruthy()
    }
    expect(screen.getByText('XMP')).toBeTruthy()
    expect(screen.getByText('Other')).toBeTruthy()
    expect(screen.queryByText('Producer')).toBeNull()
    expect(screen.queryByLabelText('Keywords')).toBeNull()           // not editable: no input
    expect(screen.getByRole('button', { name: 'Delete Keywords' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Delete Producer' })).toBeNull()
  })

  test('read-only format shows the reason but filesystem mtime stays editable', async () => {
    editor(rec([
      f('pdf:Title', 'T', { editable: false, deletable: false, label: 'Title' }),
      f('fs:mtime', '2026-01-02T03:04:05+00:00', { field_type: 'datetime', deletable: false, label: 'Modified' }),
    ], { format_supports_write: false, format_supports_delete: false, read_only_reason: 'Signed PDF: read-only' }))
    const notice = screen.getByTestId('read-only-notice')
    expect(notice.textContent).toContain('Signed PDF: read-only')
    expect(notice.textContent).toMatch(/filesystem/i)
    fireEvent.change(screen.getByLabelText('Modified'), { target: { value: '2026-02-02T03:04:05' } })
    const [, fields] = await save()
    expect(fields).toEqual([{ key: 'fs:mtime', value: '2026-02-02T03:04:05+00:00' }])
  })

  test('no read-only notice when the format is writable', () => {
    editor(rec([f('pdf:Title', 'T')]))
    expect(screen.queryByTestId('read-only-notice')).toBeNull()
  })

  test('read errors are a blocking alert, warnings are discreet', () => {
    editor(rec([f('fs:mtime', '2026-01-02T03:04:05+00:00', { field_type: 'datetime' })], {
      read_errors: ['EXIF: truncated segment'],
      warnings: ['No format-specific metadata available', 'MakerNote is preserved but not editable'],
    }))
    const blocking = screen.getByTestId('read-errors')
    expect(blocking.getAttribute('role')).toBe('alert')
    expect(blocking.textContent).toContain('EXIF: truncated segment')
    const warnings = screen.getByTestId('warnings')
    expect(warnings.getAttribute('role')).toBe('status')
    expect(warnings.textContent).toContain('MakerNote is preserved but not editable')
    const quiet = screen.getByTestId('quiet-notice')
    expect(quiet.textContent).toBe('No format-specific metadata available')
    expect(quiet.closest('[data-testid="warnings"]')).toBeNull()
  })
})

describe('add field', () => {
  const addable = [
    f('tag:TIT2', null, { label: 'Title' }),
    f('tag:TCON', null, { label: 'Genre', multi: true }),
    f('tag:TBPM', null, { label: 'BPM', field_type: 'int' }),
  ]

  test('adds a server-declared field with a typed input and sends it with /write', async () => {
    editor(rec([f('tag:TPE1', ['A'], { multi: true, label: 'Artist' })], { addable_fields: addable }))
    const choose = screen.getByLabelText('Field to add')
    expect([...choose.options].map(o => o.value)).toEqual(['', 'tag:TIT2', 'tag:TCON', 'tag:TBPM'])
    fireEvent.change(choose, { target: { value: 'tag:TBPM' } })
    fireEvent.change(screen.getByLabelText('New BPM'), { target: { value: '99' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add field' }))
    // Once added it is a pending change and no longer offered.
    expect([...screen.getByLabelText('Field to add').options].map(o => o.value)).toEqual(['', 'tag:TIT2', 'tag:TCON'])
    expect(row('tag:TBPM')).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Field to add'), { target: { value: 'tag:TCON' } })
    fireEvent.change(screen.getByLabelText('New Genre [1]'), { target: { value: 'Jazz' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add field' }))
    const [, fields] = await save()
    expect(fields).toEqual([{ key: 'tag:TBPM', value: 99 }, { key: 'tag:TCON', value: ['Jazz'] }])
  })

  test('a pending addition can be removed and the section is hidden without addable fields', () => {
    const view = editor(rec([f('tag:TPE1', 'A')], { addable_fields: addable }))
    fireEvent.change(screen.getByLabelText('Field to add'), { target: { value: 'tag:TIT2' } })
    fireEvent.change(screen.getByLabelText('New Title'), { target: { value: 'X' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add field' }))
    expect(screen.getByText('1 unsaved change')).toBeTruthy()
    fireEvent.click(within(row('tag:TIT2')).getByRole('button', { name: 'Remove new Title' }))
    expect(row('tag:TIT2')).toBeNull()
    expect(screen.queryByText(/unsaved change/)).toBeNull()
    view.rerender(<MetadataEditor {...view.props} record={rec([f('tag:TPE1', 'A')])} />)
    expect(screen.queryByLabelText('Field to add')).toBeNull()
  })
})

describe('saves without an undo entry', () => {
  const undoNote = { code: 'undo_too_large', message: 'This change was saved but is too large to be undone (70 MiB > 64 MiB limit)' }

  test('a saved change that cannot be undone is flagged, not shown as a plain "Saved"', async () => {
    metalens.write.mockResolvedValue({ ok: true, undo_token: null, undo_unavailable: undoNote })
    const { props } = editor(rec([f('tag:COMM', 'x', { label: 'Comment' })]))
    fireEvent.change(screen.getByLabelText('Comment'), { target: { value: 'y' } })
    await save()
    const note = await screen.findByRole('status')
    expect(note.textContent).toContain('too large to be undone')
    expect(props.onUndoPush).not.toHaveBeenCalled()
    expect(props.onSaved).toHaveBeenCalled()
  })

  test('server warnings of a successful save are shown with the saved state', async () => {
    metalens.write.mockResolvedValue({ ok: true, undo_token: 'tok', undo_unavailable: null,
      warnings: ['Could not restore the file owner (privilege missing)'] })
    editor(rec([f('tag:COMM', 'x', { label: 'Comment' })]))
    fireEvent.change(screen.getByLabelText('Comment'), { target: { value: 'y' } })
    await save()
    const note = await screen.findByRole('status')
    expect(note.textContent).toContain('Could not restore the file owner')
    await screen.findByText('Saved ✓')
  })

  test('a genuine no-op save shows no warning', async () => {
    metalens.write.mockResolvedValue({ ok: true, undo_token: null, undo_unavailable: null })
    editor(rec([f('tag:COMM', 'x', { label: 'Comment' })]))
    fireEvent.change(screen.getByLabelText('Comment'), { target: { value: 'y' } })
    await save()
    await screen.findByText('Saved ✓')
    expect(screen.queryByRole('status')).toBeNull()
  })

  test('the warning is cleared by the next edit', async () => {
    metalens.write.mockResolvedValue({ ok: true, undo_token: null, undo_unavailable: undoNote })
    editor(rec([f('tag:COMM', 'x', { label: 'Comment' })]))
    fireEvent.change(screen.getByLabelText('Comment'), { target: { value: 'y' } })
    await save()
    await screen.findByRole('status')
    fireEvent.change(screen.getByLabelText('Comment'), { target: { value: 'z' } })
    expect(screen.queryByRole('status')).toBeNull()
  })
})
