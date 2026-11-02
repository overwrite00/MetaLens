import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import { StrictMode, useEffect, useState } from 'react'
import { MetadataEditor } from '../src/components/MetadataEditor'
import { DetailPanel } from '../src/components/DetailPanel'
import { FilePanel } from '../src/components/FilePanel'
import { DiffView, makeCsv, exportCsv } from '../src/components/DiffView'
import { FolderPanel } from '../src/components/FolderPanel'
import { useUndoStack } from '../src/hooks/useUndoStack'
import { metalens } from '../src/api/client'
import { apiError, connectFakeSidecar, createFakeSidecar } from './fake-sidecar'

vi.mock('../src/api/client', () => ({ metalens: { write: vi.fn(), delete: vi.fn(), undo: vi.fn(), read: vi.fn(), list: vi.fn(), hash: vi.fn(), diff: vi.fn() } }))
const field = (value = 'original') => ({ key: 'pdf:Title', label: 'Title', value, editable: true, deletable: true })
const record = (value = 'original') => ({ supports_write: true, fields: [field(value)] })
function deferred() { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b }); return { promise, resolve, reject } }
beforeEach(() => { vi.resetAllMocks(); metalens.write.mockResolvedValue({ ok: true, errors: [], undo_token: null }); metalens.delete.mockResolvedValue({ ok: true, errors: [], undo_token: null }) })

test('the undo entry comes from the server token, pushed only after a successful save', async () => {
  const push = vi.fn(); const write = deferred(); metalens.write.mockReturnValue(write.promise)
  render(<MetadataEditor record={record()} filePath="a.pdf" onSaved={vi.fn()} onUndoPush={push} />)
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'edited' } })
  fireEvent.click(screen.getByText('Save'))
  expect(push).not.toHaveBeenCalled()
  await act(async () => write.resolve({ ok: true, errors: [], undo_token: 'tok-1' }))
  expect(push).toHaveBeenCalledExactlyOnceWith({ filePath: 'a.pdf', token: 'tok-1', label: 'Edit Title' })
  expect(metalens.write).toHaveBeenCalledExactlyOnceWith('a.pdf', [{ key: 'pdf:Title', value: 'edited' }], [])
})

test('failed writes create no undo entry', async () => {
  const push = vi.fn(); metalens.write.mockRejectedValueOnce(new Error('write failed'))
  render(<MetadataEditor record={record()} filePath="a.pdf" onSaved={vi.fn()} onUndoPush={push} />)
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'edited' } }); fireEvent.click(screen.getByText('Save'))
  await screen.findByText('write failed'); expect(push).not.toHaveBeenCalled()
})

test('a no-op save (null token) refreshes but creates no undo entry', async () => {
  const push = vi.fn(), saved = vi.fn(); metalens.write.mockResolvedValue({ ok: true, errors: [], undo_token: null })
  render(<MetadataEditor record={record()} filePath="a.pdf" onSaved={saved} onUndoPush={push} />)
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'typo' } })
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'original' } }); fireEvent.click(screen.getByText('Save'))
  await screen.findByText('Saved ✓')
  expect(metalens.write).toHaveBeenCalledExactlyOnceWith('a.pdf', [{ key: 'pdf:Title', value: 'original' }], [])
  expect(saved).toHaveBeenCalledTimes(1); expect(push).not.toHaveBeenCalled()
})

test('edits and deletions are one atomic write with one undo entry; a failed save is retried whole', async () => {
  const push = vi.fn()
  const author = { ...field('author'), key: 'pdf:Author', label: 'Author' }
  metalens.write.mockRejectedValueOnce(new Error('save failed')).mockResolvedValueOnce({ ok: true, errors: [], undo_token: 'tok-9' })
  render(<MetadataEditor record={{ supports_write: true, fields: [field(), author] }} filePath="a.pdf" onSaved={vi.fn()} onUndoPush={push} />)
  fireEvent.change(screen.getAllByRole('textbox')[0], { target: { value: 'edited' } })
  fireEvent.click(screen.getByRole('button', { name: 'Delete Author' })); fireEvent.click(screen.getByText('Save'))
  await screen.findByText('save failed')
  expect(push).not.toHaveBeenCalled()   // nothing was saved: no partial undo entry
  fireEvent.click(screen.getByText('Save'))
  await screen.findByText('Saved ✓')
  const call = ['a.pdf', [{ key: 'pdf:Title', value: 'edited' }], ['pdf:Author']]
  expect(metalens.write.mock.calls).toEqual([call, call])
  expect(metalens.delete).not.toHaveBeenCalled()
  expect(push).toHaveBeenCalledExactlyOnceWith({ filePath: 'a.pdf', token: 'tok-9', label: 'Edit Title; Delete Author' })
})

test('a changed then deleted field is deleted once without an intermediate write', async () => {
  const push = vi.fn(); metalens.write.mockResolvedValue({ ok: true, errors: [], undo_token: 'tok-1' })
  render(<MetadataEditor record={record()} filePath="a.pdf" onSaved={vi.fn()} onUndoPush={push} />)
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'edited' } })
  fireEvent.click(screen.getByRole('button', { name: 'Delete Title' })); fireEvent.click(screen.getByText('Save'))
  await waitFor(() => expect(push).toHaveBeenCalled())
  expect(metalens.write).toHaveBeenCalledExactlyOnceWith('a.pdf', [], ['pdf:Title'])
  expect(metalens.delete).not.toHaveBeenCalled()
  expect(push).toHaveBeenCalledWith({ filePath: 'a.pdf', token: 'tok-1', label: 'Delete Title' })
})

test('repeated save click submits a single write and ignores completion after a file switch', async () => {
  const write = deferred(), saved = vi.fn(), push = vi.fn(); metalens.write.mockReturnValue(write.promise)
  const view = render(<MetadataEditor record={record()} filePath="a.pdf" onSaved={saved} onUndoPush={push} />)
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'edited' } })
  fireEvent.click(screen.getByText('Save')); fireEvent.click(screen.getByText('Saving…'))
  view.rerender(<MetadataEditor record={record('second')} filePath="b.pdf" onSaved={saved} onUndoPush={push} />)
  await act(async () => write.resolve({ ok: true, errors: [], undo_token: 'tok-1' }))
  expect(metalens.write).toHaveBeenCalledTimes(1); expect(saved).not.toHaveBeenCalled()
  // The save did happen on a.pdf: its undo entry still belongs to a.pdf.
  expect(push).toHaveBeenCalledExactlyOnceWith({ filePath: 'a.pdf', token: 'tok-1', label: 'Edit Title' })
})

test('mutation queue orders a pending save before undo and recovers after failure', async () => {
  let stack
  function Harness() { stack = useUndoStack(); return null }
  render(<Harness />)
  const save = deferred(), order = []
  let first, second
  act(() => {
    first = stack.runExclusive(async () => { order.push('save'); await save.promise; stack.push({ filePath: 'a' }) })
    second = stack.undo(async () => { order.push('undo') })
  })
  await act(async () => { await Promise.resolve() })
  expect(order).toEqual(['save'])
  await act(async () => { save.resolve(); await Promise.all([first, second]) })
  expect(order).toEqual(['save', 'undo']); expect(stack.canUndo()).toBe(false)
})

test('undo stack keeps the most recent fifty operations', async () => {
  let stack
  function Harness() { stack = useUndoStack(); return null }
  render(<Harness />)
  act(() => { for (let n = 0; n < 52; n++) stack.push({ n }) })
  const restore = vi.fn()
  await act(async () => { for (let n = 0; n < 52; n++) await stack.undo(restore) })
  expect(restore).toHaveBeenCalledTimes(50)
  expect(restore.mock.calls[0][0].n).toBe(51); expect(restore.mock.calls[49][0].n).toBe(2)
})

test.each(['=2+2', '+cmd', '-2+2', '@SUM(A1)', ' \t=3', '\r=4', '＝2', '＋2', '－2', '＠A1'])('CSV formula-like values are text: %j', value => {
  const csv = makeCsv({ ...diff(value), only_in_b: [field(value)] }, value, 'normal')
  expect(csv).toContain(`"'${value}"`)
})

test('CSV escapes quotes and line breaks and revokes the download URL', async () => {
  const value = 'a,"b"\nc'
  expect(makeCsv(diff(value), 'a', 'b')).toContain('"a,""b""\nc"')
  const create = vi.fn().mockReturnValue('blob:test'), revoke = vi.fn()
  vi.stubGlobal('URL', { createObjectURL: create, revokeObjectURL: revoke })
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  exportCsv(diff('safe'), 'a', 'b')
  expect(click).toHaveBeenCalledOnce(); expect(document.querySelector('a')).toBeNull()
  await waitFor(() => expect(revoke).toHaveBeenCalledWith('blob:test'))
  click.mockRestore(); vi.unstubAllGlobals()
})

function UndoHarness() {
  const [data, setData] = useState(null)
  const stack = useUndoStack()
  const reload = async () => setData(await metalens.read('a.pdf'))
  useEffect(() => { reload() }, [])
  return <>
    <MetadataEditor record={data} filePath="a.pdf" onSaved={reload} onUndoPush={stack.push} runExclusive={stack.runExclusive} mutationBusy={stack.busy} />
    <button disabled={!stack.canUndo() || stack.busy} onClick={async () => {
      try { await stack.undo(entry => metalens.undo(entry.token)) } catch { /* App shows the reason */ }
      await reload()
    }}>Restore</button>
  </>
}

test('deleted fields are restored by the token undo', async () => {
  const server = connectFakeSidecar(metalens, createFakeSidecar({ fields: [field()] }))
  render(<UndoHarness />)
  fireEvent.click(await screen.findByRole('button', { name: 'Delete Title' })); fireEvent.click(screen.getByText('Save'))
  await screen.findByText('No editable fields')
  expect(server.values.has('pdf:Title')).toBe(false)
  await waitFor(() => expect(screen.getByText('Restore').disabled).toBe(false))
  fireEvent.click(screen.getByText('Restore'))
  await waitFor(() => expect(screen.getByRole('textbox').value).toBe('original'))
  expect(metalens.undo).toHaveBeenCalledExactlyOnceWith('tok-1')
  expect(server.values.get('pdf:Title')).toBe('original'); expect(server.journal.size).toBe(0)
})

test('edit and delete each round-trip through the token undo and record reload', async () => {
  const server = connectFakeSidecar(metalens, createFakeSidecar({ fields: [field()] }))
  render(<UndoHarness />)
  fireEvent.change(await screen.findByRole('textbox'), { target: { value: 'changed' } }); fireEvent.click(screen.getByText('Save'))
  await waitFor(() => expect(screen.getByRole('textbox').value).toBe('changed'))
  expect(server.values.get('pdf:Title')).toBe('changed')
  await waitFor(() => expect(screen.getByText('Restore').disabled).toBe(false))
  fireEvent.click(screen.getByText('Restore'))
  await waitFor(() => expect(screen.getByRole('textbox').value).toBe('original'))
  // The server schema survives the round-trip: still editable and deletable.
  fireEvent.click(screen.getByRole('button', { name: 'Delete Title' })); fireEvent.click(screen.getByText('Save'))
  await screen.findByText('No editable fields')
  expect(server.values.has('pdf:Title')).toBe(false)
  await waitFor(() => expect(screen.getByText('Restore').disabled).toBe(false))
  fireEvent.click(screen.getByText('Restore'))
  await waitFor(() => expect(screen.getByRole('textbox').value).toBe('original'))
  expect(metalens.write.mock.calls).toEqual([
    ['a.pdf', [{ key: 'pdf:Title', value: 'changed' }], []],
    ['a.pdf', [], ['pdf:Title']],
  ])
  expect(metalens.undo.mock.calls).toEqual([['tok-1'], ['tok-2']])
  expect(server.values.get('pdf:Title')).toBe('original'); expect(server.journal.size).toBe(0)
  expect(screen.getByText('Restore').disabled).toBe(true)
})

test('an added field is removed again by undo', async () => {
  const subject = { key: 'pdf:Subject', label: 'Subject', value: null, editable: true, deletable: true, field_type: 'str' }
  const server = connectFakeSidecar(metalens, createFakeSidecar({ fields: [field()], addable: [subject] }))
  render(<UndoHarness />)
  fireEvent.change(await screen.findByLabelText('Field to add'), { target: { value: 'pdf:Subject' } })
  fireEvent.change(screen.getByLabelText('New Subject'), { target: { value: 'topic' } })
  fireEvent.click(screen.getByRole('button', { name: 'Add field' })); fireEvent.click(screen.getByText('Save'))
  await waitFor(() => expect(server.values.get('pdf:Subject')).toBe('topic'))
  expect(metalens.write).toHaveBeenCalledExactlyOnceWith('a.pdf', [{ key: 'pdf:Subject', value: 'topic' }], [])
  await waitFor(() => expect(screen.getByText('Restore').disabled).toBe(false))
  fireEvent.click(screen.getByText('Restore'))
  await waitFor(() => expect(server.values.has('pdf:Subject')).toBe(false))
  await waitFor(() => expect(screen.getAllByRole('textbox')).toHaveLength(1))
  expect(metalens.undo).toHaveBeenCalledExactlyOnceWith('tok-1')
  expect(server.values.get('pdf:Title')).toBe('original')
})

test('undo state reacts, retries failed undo, and serializes two requests', async () => {
  let stack
  function Harness() { stack = useUndoStack(); return <span>{stack.canUndo() ? 'available' : 'empty'}</span> }
  render(<Harness />)
  act(() => stack.push({ filePath: 'a', token: 't', label: 'x' })); expect(screen.getByText('available')).toBeTruthy()
  const restore = vi.fn().mockRejectedValueOnce(new Error('retry')).mockResolvedValueOnce({})
  await act(async () => { await expect(stack.undo(restore)).rejects.toThrow('retry') })
  expect(screen.getByText('available')).toBeTruthy()
  await act(async () => { await Promise.all([stack.undo(restore), stack.undo(restore)]) })
  expect(restore).toHaveBeenCalledTimes(2); expect(screen.getByText('empty')).toBeTruthy()
})

test.each([
  [409, 'concurrent_modification', 'changed'],
  [410, 'undo_expired', 'expired'],
])('a %i undo removes only that entry, tags the reason and does not move on by itself', async (status, code, reason) => {
  let stack
  function Harness() { stack = useUndoStack(); return null }
  render(<Harness />)
  act(() => { stack.push({ filePath: 'a', token: 'old' }); stack.push({ filePath: 'a', token: 'new' }) })
  const restore = vi.fn().mockRejectedValueOnce(apiError(status, code, 'refused')).mockResolvedValueOnce({})
  let failure
  await act(async () => { failure = await stack.undo(restore).catch(e => e) })
  expect(failure.undoDiscarded).toBe(reason); expect(failure.undoEntry.token).toBe('new')
  expect(restore).toHaveBeenCalledTimes(1); expect(stack.canUndo()).toBe(true)
  let done
  await act(async () => { done = await stack.undo(restore) })
  expect(done.token).toBe('old')
  expect(restore.mock.calls.map(([entry]) => entry.token)).toEqual(['new', 'old'])
  expect(stack.canUndo()).toBe(false)
})

test.each([
  [403, 'access_denied'], [500, 'internal_error'], [422, 'lossy_write'], [undefined, undefined],
])('a %s undo error keeps the entry on top', async (status, code) => {
  let stack
  function Harness() { stack = useUndoStack(); return null }
  render(<Harness />)
  act(() => { stack.push({ filePath: 'a', token: 'old' }); stack.push({ filePath: 'a', token: 'new' }) })
  const error = status ? apiError(status, code, 'failed') : new TypeError('Failed to fetch')
  const restore = vi.fn().mockRejectedValueOnce(error).mockResolvedValue({})
  let failure
  await act(async () => { failure = await stack.undo(restore).catch(x => x) })
  expect(failure).toBe(error); expect(failure.undoDiscarded).toBeUndefined()
  await act(async () => { await stack.undo(restore) })
  expect(restore.mock.calls.map(([entry]) => entry.token)).toEqual(['new', 'new'])
  expect(stack.canUndo()).toBe(true)
})

test('undo against the fake sidecar: external change is 409 and keeps the server entry, used token is 410', async () => {
  const server = createFakeSidecar({ fields: [field()] })
  const { undo_token } = await server.write('a.pdf', [{ key: 'pdf:Title', value: 'mine' }], [])
  server.externalEdit('pdf:Title', 'theirs')
  await expect(server.undo(undo_token)).rejects.toMatchObject({ status: 409, code: 'concurrent_modification' })
  expect(server.journal.has(undo_token)).toBe(true)
  server.externalEdit('pdf:Title', 'mine')
  await expect(server.undo(undo_token)).resolves.toMatchObject({ ok: true, keys: ['pdf:Title'], undo_token: null })
  await expect(server.undo(undo_token)).rejects.toMatchObject({ status: 410, code: 'undo_expired' })
  expect(await server.write('a.pdf', [{ key: 'pdf:Title', value: 'original' }], [])).toEqual({ ok: true, errors: [], undo_token: null })
})

test('a late read cannot overwrite a newly selected file', async () => {
  const a = deferred(), b = deferred(); metalens.read.mockImplementation(p => p === 'a.pdf' ? a.promise : b.promise)
  const view = render(<DetailPanel filePath="a.pdf" onUndoPush={vi.fn()} />)
  view.rerender(<DetailPanel filePath="b.pdf" onUndoPush={vi.fn()} />)
  await act(async () => b.resolve(record('new file')))
  await act(async () => a.resolve(record('old file')))
  expect(screen.getByText('new file')).toBeTruthy(); expect(screen.queryByText('old file')).toBeNull()
})

test('late directory listing cannot replace the selected directory', async () => {
  const a = deferred(), b = deferred(); metalens.list.mockImplementation(p => p === 'a' ? a.promise : b.promise)
  const view = render(<FilePanel currentPath="a" onSelectFile={vi.fn()} onSelectTwo={vi.fn()} />)
  view.rerender(<FilePanel currentPath="b" onSelectFile={vi.fn()} onSelectTwo={vi.fn()} />)
  await act(async () => b.resolve({ items: [{ name: 'new.pdf', path: 'b/new.pdf' }] }))
  await act(async () => a.resolve({ items: [{ name: 'old.pdf', path: 'a/old.pdf' }] }))
  expect(screen.getByText('new.pdf')).toBeTruthy(); expect(screen.queryByText('old.pdf')).toBeNull()
})

test('equal sort values remain stable and StrictMode selection emits each action once', async () => {
  metalens.list.mockResolvedValue({ items: [{ name: 'zeta.pdf', path: 'z', size: 1 }, { name: 'alpha.pdf', path: 'a', size: 1 }] })
  const select = vi.fn(), two = vi.fn()
  render(<StrictMode><FilePanel currentPath="folder" onSelectFile={select} onSelectTwo={two} /></StrictMode>)
  await screen.findByText('zeta.pdf')
  fireEvent.click(screen.getByText('Size'))
  expect(screen.getAllByText(/^(zeta|alpha)\.pdf$/).map(node => node.textContent)).toEqual(['zeta.pdf', 'alpha.pdf'])
  fireEvent.click(screen.getByText('zeta.pdf'))
  expect(select).toHaveBeenCalledExactlyOnceWith('z')
  fireEvent.click(screen.getByText('alpha.pdf'), { ctrlKey: true })
  expect(two).toHaveBeenCalledExactlyOnceWith('z', 'a')
})

const diff = label => ({ only_in_a: [{ ...field('value'), label }], only_in_b: [], changed: [], identical: [] })
test('late diff cannot replace the active pair', async () => {
  const a = deferred(), b = deferred(); metalens.diff.mockReturnValueOnce(a.promise).mockReturnValueOnce(b.promise)
  const view = render(<DiffView fileA="a" fileB="b" />); view.rerender(<DiffView fileA="c" fileB="d" />)
  await act(async () => b.resolve(diff('current'))); await act(async () => a.resolve(diff('obsolete')))
  expect(screen.getByText('current')).toBeTruthy(); expect(screen.queryByText('obsolete')).toBeNull()
})

test('late hash cannot be shown under the newly selected file', async () => {
  const hash = deferred(); metalens.read.mockResolvedValue(record()); metalens.hash.mockReturnValue(hash.promise)
  const view = render(<DetailPanel filePath="a.pdf" onUndoPush={vi.fn()} />)
  fireEvent.click(await screen.findByText('Compute Hashes'))
  view.rerender(<DetailPanel filePath="b.pdf" onUndoPush={vi.fn()} />)
  await screen.findByText('Compute Hashes'); await act(async () => hash.resolve({ hashes: { sha256: 'old hash' } }))
  expect(screen.queryByText('old hash')).toBeNull()
})

test('hash copy reports success only after clipboard resolves and handles denial', async () => {
  const copy = deferred()
  const writeText = vi.fn().mockReturnValueOnce(copy.promise).mockRejectedValueOnce(new Error('denied'))
  vi.stubGlobal('navigator', { clipboard: { writeText } })
  metalens.read.mockResolvedValue(record()); metalens.hash.mockResolvedValue({ hashes: { sha256: 'digest' } })
  render(<DetailPanel filePath="a.pdf" onUndoPush={vi.fn()} />)
  fireEvent.click(await screen.findByText('Compute Hashes'))
  fireEvent.click(await screen.findByRole('button', { name: 'Copy sha256' }))
  expect(screen.queryByRole('button', { name: 'Copied sha256' })).toBeNull()
  await act(async () => copy.resolve())
  fireEvent.click(screen.getByRole('button', { name: 'Copied sha256' }))
  await screen.findByRole('alert')
  expect(screen.getByRole('alert').textContent).toBe('Copy failed: denied')
  expect(screen.queryByRole('button', { name: 'Copied sha256' })).toBeNull()
  expect(writeText).toHaveBeenCalledWith('digest')
  vi.unstubAllGlobals()
})

test('refresh keeps expanded nodes and selection, re-reads folders and notifies the parent', async () => {
  window.electronAPI = { listDrives: vi.fn().mockResolvedValue(['C:\\']), homeDir: vi.fn().mockResolvedValue('C:\\Users') }
  const onRefresh = vi.fn()
  metalens.list.mockResolvedValueOnce({ items: [{ name: 'child', path: 'C:\\child', is_dir: true }] })
    .mockResolvedValueOnce({ items: [{ name: 'child', path: 'C:\\child', is_dir: true }, { name: 'newdir', path: 'C:\\newdir', is_dir: true }] })
  render(<FolderPanel onSelectPath={vi.fn()} onRefresh={onRefresh} />); fireEvent.click(await screen.findByText('C:\\'))
  await screen.findByText('child'); fireEvent.click(screen.getByText('Refresh'))
  await screen.findByText('newdir')
  expect(screen.getByText('child')).toBeTruthy()
  expect(metalens.list).toHaveBeenCalledTimes(2)
  await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1))
})

test('refresh moves a vanished selection to the nearest existing ancestor and keeps unreadable folders', async () => {
  window.electronAPI = { listDrives: vi.fn().mockResolvedValue(['C:\\']), homeDir: vi.fn().mockResolvedValue('C:\\Users') }
  const onSelectPath = vi.fn(), onRefresh = vi.fn()
  metalens.list.mockImplementation(async path => {
    if (path === 'C:\\') return metalens.list.drive
    if (path === 'C:\\a') throw new Error('gone')
    return { items: [] }
  })
  metalens.list.drive = { items: [{ name: 'a', path: 'C:\\a', is_dir: true }] }
  render(<FolderPanel onSelectPath={onSelectPath} onRefresh={onRefresh} />)
  fireEvent.click(await screen.findByText('C:\\')); fireEvent.click(await screen.findByText('a'))
  metalens.list.drive = { items: [] }
  fireEvent.click(screen.getByText('Refresh'))
  await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1))
  expect(screen.queryByText('a')).toBeNull()
  expect(onSelectPath).toHaveBeenLastCalledWith('C:\\')
})

test('folder errors are visible and retry loads children', async () => {
  window.electronAPI = { listDrives: vi.fn().mockResolvedValue(['C:\\']), homeDir: vi.fn().mockResolvedValue('C:\\Users') }
  metalens.list.mockRejectedValueOnce(new Error('Access denied')).mockResolvedValueOnce({ items: [{ name: 'child', path: 'C:\\child', is_dir: true }] })
  render(<FolderPanel onSelectPath={vi.fn()} />)
  fireEvent.click(await screen.findByText('C:\\')); expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Access denied')
  fireEvent.click(screen.getByText('C:\\')); await screen.findByText('child'); expect(screen.queryByRole('alert')).toBeNull()
})
