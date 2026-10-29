import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import { StrictMode, useState } from 'react'
import { MetadataEditor } from '../src/components/MetadataEditor'
import { DetailPanel } from '../src/components/DetailPanel'
import { FilePanel } from '../src/components/FilePanel'
import { DiffView, makeCsv, exportCsv } from '../src/components/DiffView'
import { FolderPanel } from '../src/components/FolderPanel'
import { useUndoStack } from '../src/hooks/useUndoStack'
import { metalens } from '../src/api/client'

vi.mock('../src/api/client', () => ({ metalens: { write: vi.fn(), delete: vi.fn(), read: vi.fn(), list: vi.fn(), hash: vi.fn(), diff: vi.fn() } }))
const field = (value = 'original') => ({ key: 'pdf:Title', label: 'Title', value, editable: true, deletable: true })
const record = (value = 'original') => ({ supports_write: true, fields: [field(value)] })
function deferred() { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b }); return { promise, resolve, reject } }
beforeEach(() => { vi.resetAllMocks(); metalens.write.mockResolvedValue({}); metalens.delete.mockResolvedValue({}) })

test('undo captures originals only after a successful write', async () => {
  const push = vi.fn(); const write = deferred(); metalens.write.mockReturnValue(write.promise)
  render(<MetadataEditor record={record()} filePath="a.pdf" onSaved={vi.fn()} onUndoPush={push} />)
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'edited' } })
  fireEvent.click(screen.getByText('Save'))
  expect(push).not.toHaveBeenCalled()
  await act(async () => write.resolve({}))
  expect(push).toHaveBeenCalledWith({ filePath: 'a.pdf', fields: [field()] })
})

test('failed writes create no undo entry', async () => {
  const push = vi.fn(); metalens.write.mockRejectedValueOnce(new Error('write failed'))
  render(<MetadataEditor record={record()} filePath="a.pdf" onSaved={vi.fn()} onUndoPush={push} />)
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'edited' } }); fireEvent.click(screen.getByText('Save'))
  await screen.findByText('write failed'); expect(push).not.toHaveBeenCalled()
})

test('a failed delete preserves undo of successful edits and allows retry', async () => {
  const push = vi.fn()
  const author = { ...field('author'), key: 'pdf:Author', label: 'Author' }
  metalens.delete.mockRejectedValueOnce(new Error('delete failed')).mockResolvedValueOnce({})
  render(<MetadataEditor record={{ supports_write: true, fields: [field(), author] }} filePath="a.pdf" onSaved={vi.fn()} onUndoPush={push} />)
  fireEvent.change(screen.getAllByRole('textbox')[0], { target: { value: 'edited' } })
  fireEvent.click(screen.getByRole('button', { name: 'Delete Author' })); fireEvent.click(screen.getByText('Save'))
  await screen.findByText('delete failed')
  expect(push).toHaveBeenCalledWith({ filePath: 'a.pdf', fields: [field()] })
  fireEvent.click(screen.getByText('Save'))
  await screen.findByText('Saved ✓')
  expect(metalens.write).toHaveBeenCalledTimes(1)
  expect(push).toHaveBeenLastCalledWith({ filePath: 'a.pdf', fields: [author] })
})

test('a changed then deleted field is deleted once without an intermediate write', async () => {
  const push = vi.fn(); render(<MetadataEditor record={record()} filePath="a.pdf" onSaved={vi.fn()} onUndoPush={push} />)
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'edited' } })
  fireEvent.click(screen.getByRole('button', { name: 'Delete Title' })); fireEvent.click(screen.getByText('Save'))
  await waitFor(() => expect(push).toHaveBeenCalled())
  expect(metalens.write).not.toHaveBeenCalled(); expect(metalens.delete).toHaveBeenCalledWith('a.pdf', ['pdf:Title'])
})

test('repeated save click submits a single write and ignores completion after a file switch', async () => {
  const write = deferred(), saved = vi.fn(), push = vi.fn(); metalens.write.mockReturnValue(write.promise)
  const view = render(<MetadataEditor record={record()} filePath="a.pdf" onSaved={saved} onUndoPush={push} />)
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'edited' } })
  fireEvent.click(screen.getByText('Save')); fireEvent.click(screen.getByText('Saving…'))
  view.rerender(<MetadataEditor record={record('second')} filePath="b.pdf" onSaved={saved} onUndoPush={push} />)
  await act(async () => write.resolve({}))
  expect(metalens.write).toHaveBeenCalledTimes(1); expect(saved).not.toHaveBeenCalled()
  expect(push).toHaveBeenCalledWith({ filePath: 'a.pdf', fields: [field()] })
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

test('deleted fields are included in undo', async () => {
  const push = vi.fn(); render(<MetadataEditor record={record()} filePath="a.pdf" onSaved={vi.fn()} onUndoPush={push} />)
  fireEvent.click(screen.getByRole('button', { name: 'Delete Title' })); fireEvent.click(screen.getByText('Save'))
  await waitFor(() => expect(push).toHaveBeenCalledWith({ filePath: 'a.pdf', fields: [field()] }))
})

test('edit and delete each round-trip through undo and record reload', async () => {
  let stored = field()
  metalens.read.mockImplementation(async () => ({ supports_write: true, fields: stored ? [{ ...stored }] : [] }))
  metalens.write.mockImplementation(async (_path, fields) => { stored = { ...fields[0] }; return {} })
  metalens.delete.mockImplementation(async () => { stored = null; return {} })
  function Harness() {
    const [data, setData] = useState(record())
    const stack = useUndoStack()
    const reload = async () => setData(await metalens.read('a.pdf'))
    return <>
      <MetadataEditor record={data} filePath="a.pdf" onSaved={reload} onUndoPush={stack.push} runExclusive={stack.runExclusive} mutationBusy={stack.busy} />
      <button disabled={!stack.canUndo() || stack.busy} onClick={async () => { await stack.undo(entry => metalens.write(entry.filePath, entry.fields)); await reload() }}>Restore</button>
    </>
  }
  render(<Harness />)
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'changed' } }); fireEvent.click(screen.getByText('Save'))
  await waitFor(() => expect(screen.getByRole('textbox').value).toBe('changed'))
  fireEvent.click(screen.getByText('Restore'))
  await waitFor(() => expect(screen.getByRole('textbox').value).toBe('original'))
  fireEvent.click(screen.getByRole('button', { name: 'Delete Title' })); fireEvent.click(screen.getByText('Save'))
  await screen.findByText('No editable fields')
  expect(stored).toBeNull()
  fireEvent.click(screen.getByText('Restore'))
  await waitFor(() => expect(screen.getByRole('textbox').value).toBe('original'))
  expect(metalens.write).toHaveBeenCalledTimes(3)
  expect(stored).toEqual(field())
})

test('undo state reacts, retries failed undo, and serializes two requests', async () => {
  let stack
  function Harness() { stack = useUndoStack(); return <span>{stack.canUndo() ? 'available' : 'empty'}</span> }
  render(<Harness />)
  act(() => stack.push({ filePath: 'a', fields: [] })); expect(screen.getByText('available')).toBeTruthy()
  const restore = vi.fn().mockRejectedValueOnce(new Error('retry')).mockResolvedValueOnce({})
  await act(async () => { await expect(stack.undo(restore)).rejects.toThrow('retry') })
  expect(screen.getByText('available')).toBeTruthy()
  await act(async () => { await Promise.all([stack.undo(restore), stack.undo(restore)]) })
  expect(restore).toHaveBeenCalledTimes(2); expect(screen.getByText('empty')).toBeTruthy()
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
