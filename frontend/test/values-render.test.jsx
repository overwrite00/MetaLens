import { render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'
import { metalens } from '../src/api/client'
import { formatValue, formatDuration } from '../src/lib/formatValue'
import { MetadataTable } from '../src/components/MetadataTable'
import { DiffView, makeCsv } from '../src/components/DiffView'

const SHA = 'ab'.repeat(32)
const binary = (size = 12345, mime = 'image/jpeg') => ({ size, sha256: SHA, mime })
const f = (key, value, extra = {}) => ({ key, label: key.split(':').pop(), value, raw_value: value,
  editable: true, deletable: true, field_type: 'str', source: key.split(':')[0], choices: [], multi: false,
  length: null, binary: null, ...extra })

const artists = f('tag:TPE1', ['Alice', 'Bob'], { multi: true, source: 'id3' })
const cover = f('tag:APIC', binary(), { field_type: 'binary', editable: false, binary: binary(), source: 'id3' })
const length = f('audio:length', 3725.5, { field_type: 'duration', editable: false, deletable: false, source: 'audio' })
const created = f('pdf:CreationDate', '2026-01-02T03:04:05.123456+02:00', { field_type: 'datetime', source: 'pdf' })
const shot = f('exif:DateTimeOriginal', '2026-01-02T03:04:05', { field_type: 'localdatetime', source: 'exif' })

function response(status, body, statusText = 'Error', raw) {
  return {
    ok: status >= 200 && status < 300, status, statusText,
    json: async () => { if (body === undefined) throw new SyntaxError('not json'); return body },
    // The client reads the raw text (to keep integers beyond 2^53 exact); `raw` overrides it.
    text: async () => (typeof raw === 'string' ? raw : (body === undefined ? 'not json' : JSON.stringify(body))),
  }
}

describe('client errors', () => {
  beforeEach(() => {
    window.electronAPI = { getSidecarConnection: vi.fn().mockResolvedValue({ port: 4321, token: 'tok' }) }
  })
  afterEach(() => vi.unstubAllGlobals())

  test('a FastAPI detail array never becomes [object Object]', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(422, {
      detail: [{ loc: ['body', 'path'], msg: 'Field required', type: 'missing' }],
    })))
    const err = await metalens.read('a.pdf').catch(e => e)
    expect(err).toBeInstanceOf(Error)
    expect(err.message).not.toContain('[object Object]')
    expect(err.message).toContain('Field required')
    expect(err.status).toBe(422)
    expect(err.errors).toEqual([{ code: 'missing', key: 'body.path', message: 'body.path: Field required' }])
  })

  test('the uniform error body sets message, status, code and errors', async () => {
    const errors = [
      { code: 'read_only', key: 'pdf:Title', message: 'pdf:Title: field is read-only' },
      { code: 'invalid_value', key: 'pdf:Author', message: 'pdf:Author: not a string' },
    ]
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(422, { ok: false, errors, detail: 'two problems' })))
    const err = await metalens.write('a.pdf', [{ key: 'pdf:Title', value: 'x' }]).catch(e => e)
    expect(err.message).toBe('two problems')
    expect(err.status).toBe(422)
    expect(err.code).toBe('read_only')
    expect(err.errors).toEqual(errors)
  })

  test('errors without detail are joined; non-JSON bodies fall back to the status text', async () => {
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(response(409, { ok: false, errors: [{ code: 'concurrent_modification', key: null, message: 'changed' }, { message: { nested: true } }] }))
      .mockResolvedValueOnce(response(500, undefined, 'Internal Server Error')))
    const first = await metalens.read('a').catch(e => e)
    expect(first.message).toBe('changed; {"nested":true}')
    expect(first.status).toBe(409)
    expect(first.code).toBe('concurrent_modification')
    const second = await metalens.read('a').catch(e => e)
    expect(second.message).toBe('Internal Server Error')
    expect(second.status).toBe(500)
    expect(second.errors).toEqual([])
  })

  test('write sends only key and value, arrays stay arrays, and an optional delete list', async () => {
    const fetch = vi.fn().mockResolvedValue(response(200, { ok: true, errors: [] }))
    vi.stubGlobal('fetch', fetch)
    await metalens.write('a.mp3', [{ ...artists, value: ['A', 'B'] }, { key: 'tag:TBPM', value: 120 }])
    await metalens.write('a.mp3', [{ key: 'tag:TIT2', value: 'T' }], ['tag:COMM'])
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({
      path: 'a.mp3', fields: [{ key: 'tag:TPE1', value: ['A', 'B'] }, { key: 'tag:TBPM', value: 120 }],
    })
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({
      path: 'a.mp3', fields: [{ key: 'tag:TIT2', value: 'T' }], delete: ['tag:COMM'],
    })
    expect(fetch.mock.calls[0][1].headers.Authorization).toBe('Bearer tok')
  })

  test('write and delete return the undo token; undo posts only the token', async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(response(200, { ok: true, errors: [], undo_token: 'tok-1' }))
      .mockResolvedValueOnce(response(200, { ok: true, errors: [], undo_token: null }))
      .mockResolvedValueOnce(response(200, { ok: true, errors: [], keys: ['pdf:Title'], undo_token: null }))
      .mockResolvedValueOnce(response(410, { ok: false, errors: [{ code: 'undo_expired', key: null, message: 'Undo is no longer available' }] }))
    vi.stubGlobal('fetch', fetch)
    expect((await metalens.write('a.pdf', [], ['pdf:Title'])).undo_token).toBe('tok-1')
    expect((await metalens.delete('a.pdf', ['pdf:Author'])).undo_token).toBeNull()
    expect(await metalens.undo('tok-1')).toEqual({ ok: true, errors: [], keys: ['pdf:Title'], undo_token: null })
    const expired = await metalens.undo('tok-1').catch(e => e)
    expect(expired).toMatchObject({ status: 410, code: 'undo_expired', message: 'Undo is no longer available' })
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ path: 'a.pdf', fields: [], delete: ['pdf:Title'] })
    expect(fetch.mock.calls[2][0]).toBe('http://127.0.0.1:4321/undo')
    expect(fetch.mock.calls[2][1].method).toBe('POST')
    expect(JSON.parse(fetch.mock.calls[2][1].body)).toEqual({ token: 'tok-1' })
  })
})

describe('formatValue', () => {
  test('lists, binaries, durations and date-times are readable and lossless', () => {
    expect(formatValue(artists)).toBe('Alice; Bob')
    expect(formatValue(f('mp4:trkn', [3, 10], { field_type: 'int', multi: true, length: 2 }))).toBe('3; 10')
    const text = formatValue(cover, { full: true })
    expect(text).toContain('12345 bytes')
    expect(text).toContain('image/jpeg')
    expect(text).toContain(SHA)
    expect(formatValue(cover)).not.toContain(SHA)          // abbreviated on screen
    expect(formatValue(f('mp4:covr', [binary(10, null), binary(20)], { field_type: 'binary', multi: true })))
      .toMatch(/10 bytes.*; .*20 bytes/)
    expect(formatValue(length)).toBe('1:02:05.5')
    expect(formatDuration(45.25)).toBe('45.25 s')
    expect(formatDuration(125)).toBe('2:05')
    expect(formatValue({ ...length, value: 'n/a', raw_value: 'PT1H' })).toBe('PT1H')
    expect(formatValue(created)).toBe('2026-01-02 03:04:05.123456+02:00')
    expect(formatValue(shot)).toBe('2026-01-02 03:04:05')
    expect(formatValue(f('pdf:encrypted', false, { field_type: 'bool' }))).toBe('false')
    expect(formatValue(f('x:obj', { a: 1 }))).toBe('{"a":1}')
    expect(formatValue(f('x:null', null))).toBe('')
  })
})

describe('MetadataTable', () => {
  test('shows list items, binary descriptors and durations without [object Object]', () => {
    const errors = vi.spyOn(console, 'error').mockImplementation(() => {})
    const { container } = render(<MetadataTable fields={[artists, cover, length, created, artists]} />)
    expect(container.textContent).not.toContain('[object Object]')
    expect(screen.getAllByText('Alice').length).toBe(2)
    expect(screen.getAllByText('Bob').length).toBe(2)
    expect(container.textContent).toContain('12345 bytes')
    expect(container.textContent).toContain('image/jpeg')
    expect(screen.getByText('1:02:05.5').getAttribute('title')).toContain('3725.5')
    expect(container.textContent).toContain('2026-01-02 03:04:05.123456+02:00')
    expect(errors.mock.calls.flat().join(' ')).not.toMatch(/same key/i)
    errors.mockRestore()
  })
})

describe('DiffView and CSV', () => {
  const diff = {
    only_in_a: [cover, cover],
    only_in_b: [length, length],
    changed: [{ field_a: artists, field_b: { ...artists, value: ['Alice', 'Carol'] } },
      { field_a: artists, field_b: { ...artists, value: ['Dan'] } }],
    identical: [created],
  }

  test('diff rows render lists and binaries with unique React keys', async () => {
    const errors = vi.spyOn(console, 'error').mockImplementation(() => {})
    const diffFn = vi.spyOn(metalens, 'diff').mockResolvedValue(diff)
    const { container } = render(<DiffView fileA="a.mp3" fileB="b.mp3" />)
    await screen.findByText('Only in A')
    expect(container.textContent).not.toContain('[object Object]')
    expect(screen.getByText('Alice; Carol')).toBeTruthy()
    expect(screen.getByText('Dan')).toBeTruthy()
    expect(within(container).getAllByText(/12345 bytes/).length).toBe(2)
    expect(errors.mock.calls.flat().join(' ')).not.toMatch(/same key/i)
    diffFn.mockRestore(); errors.mockRestore()
  })

  test('CSV cells use the same formatting, with full hashes', () => {
    const csv = makeCsv(diff, 'a', 'b')
    expect(csv).not.toContain('[object Object]')
    expect(csv).toContain(`"TPE1","${formatValue(artists)}","Alice; Carol","changed"`)
    expect(csv).toContain(`"APIC","${formatValue(cover, { full: true })}","","only_in_a"`)
    expect(csv).toContain(SHA)
    expect(csv).toContain('"length","","1:02:05.5","only_in_b"')
    expect(csv).toContain('"CreationDate","2026-01-02 03:04:05.123456+02:00","2026-01-02 03:04:05.123456+02:00","identical"')
  })
})

describe('integers beyond 2^53 stay exact', () => {
  beforeEach(() => {
    window.electronAPI = { getSidecarConnection: vi.fn().mockResolvedValue({ port: 4321, token: 'tok' }) }
  })
  afterEach(() => vi.unstubAllGlobals())

  test('the client keeps unsafe integers as decimal strings and safe ones as numbers', async () => {
    const raw = '{"fields":[{"key":"mp4:plID","value":1152921504606846979},{"key":"a","value":42},'
      + '{"key":"q","value":[18446744073709551615,7]},{"key":"f","value":1.5e30}]}'
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(200, undefined, 'OK', raw)))
    const record = await metalens.read('a.m4a')
    expect(record.fields.map(f => f.value)).toEqual(['1152921504606846979', 42, ['18446744073709551615', 7], 1.5e30])
  })
})
