import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../src/App'
import { metalens } from '../src/api/client'
import { apiError } from './fake-sidecar'

vi.mock('../src/api/client', () => ({ metalens: { write: vi.fn(), undo: vi.fn() } }))
vi.mock('../src/components/FolderPanel', () => ({ FolderPanel: ({ onSelectPath }) => <button onClick={() => onSelectPath('folder')}>Folder</button> }))
vi.mock('../src/components/FilePanel', () => ({ FilePanel: ({ onSelectFile, reloadKey }) => <>
  <button onClick={() => onSelectFile('C:\\docs\\a.pdf')}>Select file</button><span>Files: {reloadKey}</span>
</> }))
let nextToken = 1
vi.mock('../src/components/DetailPanel', () => ({ DetailPanel: ({ filePath, onUndoPush, reloadKey, runExclusive }) => <>
  <span>File: {filePath || 'none'}</span><span>Revision: {reloadKey}</span>
  <input aria-label="Title" defaultValue="draft" />
  <button onClick={() => runExclusive(async () => onUndoPush({ filePath, token: `tok-${nextToken++}`, label: 'Edit Title' }))}>Edit file</button>
</> }))

let menuUndo
beforeEach(() => {
  vi.resetAllMocks(); nextToken = 1
  window.electronAPI = { onMenuOpenFolder: vi.fn(), onMenuUndo: cb => { menuUndo = cb }, onMenuAbout: vi.fn(), removeAllListeners: vi.fn() }
})

async function appWithEdits(count) {
  render(<App />)
  const undo = screen.getByRole('button', { name: 'Undo' })
  expect(undo.disabled).toBe(true)
  fireEvent.click(screen.getByText('Select file'))
  for (let n = 0; n < count; n++) fireEvent.click(screen.getByText('Edit file'))
  await waitFor(() => expect(undo.disabled).toBe(false))
  return undo
}

test('undo sends only the token, reloads record and file list, and failed undo remains retryable', async () => {
  const undo = await appWithEdits(1)
  metalens.undo.mockRejectedValueOnce(apiError(403, 'access_denied', 'access denied'))
    .mockResolvedValueOnce({ ok: true, errors: [], keys: ['pdf:Title'], undo_token: null })
  await act(async () => menuUndo())
  await screen.findByText('Undo failed: access denied'); expect(undo.disabled).toBe(false)
  expect(screen.getByText('Revision: 0')).toBeTruthy()
  await act(async () => menuUndo())
  await screen.findByText(/Undone: Edit Title \(a\.pdf\)/)
  expect(screen.getByText('Revision: 1')).toBeTruthy(); expect(screen.getByText('Files: 1')).toBeTruthy()
  expect(undo.disabled).toBe(true)
  expect(metalens.undo.mock.calls).toEqual([['tok-1'], ['tok-1']])
  expect(metalens.write).not.toHaveBeenCalled()
  fireEvent.click(screen.getByText('Folder')); expect(screen.getByText('File: none')).toBeTruthy()
})

test.each([
  [500, 'internal_error', 'boom', /Undo failed: boom/],
  [422, 'lossy_write', 'would lose data', /Undo failed: would lose data/],
])('a %i undo error keeps the entry for retry', async (status, code, message, shown) => {
  const undo = await appWithEdits(1)
  metalens.undo.mockRejectedValueOnce(apiError(status, code, message))
  await act(async () => menuUndo())
  await screen.findByText(shown); expect(undo.disabled).toBe(false)
  metalens.undo.mockRejectedValueOnce(new TypeError('Failed to fetch'))
  fireEvent.click(undo)
  await screen.findByText('Undo failed: Failed to fetch'); expect(undo.disabled).toBe(false)
  expect(metalens.undo.mock.calls).toEqual([['tok-1'], ['tok-1']])
})

test('503 file_busy is transient: the entry stays and the undo can be retried', async () => {
  const undo = await appWithEdits(1)
  metalens.undo.mockRejectedValueOnce(apiError(503, 'file_busy', 'The file is in use by another program'))
    .mockResolvedValueOnce({ ok: true, errors: [], keys: ['pdf:Title'], undo_token: null, warnings: [] })
  await act(async () => menuUndo())
  await screen.findByText('Undo failed: The file is in use by another program')
  expect(undo.disabled).toBe(false)
  await act(async () => menuUndo())
  await screen.findByText(/Undone: Edit Title/)
  expect(metalens.undo.mock.calls).toEqual([['tok-1'], ['tok-1']])
})

test('warnings of a successful undo are shown with the confirmation', async () => {
  await appWithEdits(1)
  metalens.undo.mockResolvedValueOnce({ ok: true, errors: [], keys: ['pdf:Title'], undo_token: null,
    warnings: ['Could not restore the file owner (privilege missing)'] })
  await act(async () => menuUndo())
  await screen.findByText(/Undone: Edit Title \(a\.pdf\).*Could not restore the file owner/)
})

test('409 removes the entry with a reason and the older entry is undone only on request', async () => {
  const undo = await appWithEdits(2)
  metalens.undo.mockRejectedValueOnce(apiError(409, 'concurrent_modification', 'pdf:Title: modified since the operation'))
    .mockResolvedValueOnce({ ok: true, errors: [], keys: ['pdf:Title'], undo_token: null })
  await act(async () => menuUndo())
  await screen.findByText(/a\.pdf changed since the save.*removed/i)
  expect(metalens.undo.mock.calls).toEqual([['tok-2']])   // no automatic fallback to tok-1
  expect(undo.disabled).toBe(false)
  fireEvent.click(undo)
  await screen.findByText(/Undone: Edit Title/)
  expect(metalens.undo.mock.calls).toEqual([['tok-2'], ['tok-1']])
  expect(undo.disabled).toBe(true)
})

test('410 removes the entry and says undo is no longer available', async () => {
  const undo = await appWithEdits(1)
  metalens.undo.mockRejectedValueOnce(apiError(410, 'undo_expired', 'Undo is no longer available'))
  await act(async () => menuUndo())
  await screen.findByText(/Undo no longer available for a\.pdf.*removed/i)
  expect(undo.disabled).toBe(true)
  expect(screen.getByText('Revision: 0')).toBeTruthy()
})

test('Ctrl+Z inside a text field is the native text undo, outside it undoes the file', async () => {
  await appWithEdits(1)
  metalens.undo.mockResolvedValue({ ok: true, errors: [], keys: ['pdf:Title'], undo_token: null })
  const input = screen.getByLabelText('Title')
  input.focus()
  const inField = fireEvent.keyDown(input, { key: 'z', ctrlKey: true })
  expect(inField).toBe(true)   // default not prevented: the browser undoes the typing
  await act(async () => {})
  expect(metalens.undo).not.toHaveBeenCalled()

  const editable = document.createElement('div'); editable.setAttribute('contenteditable', 'true'); document.body.appendChild(editable)
  fireEvent.keyDown(editable, { key: 'z', ctrlKey: true })
  const area = document.createElement('textarea'); document.body.appendChild(area)
  fireEvent.keyDown(area, { key: 'Z', metaKey: true })
  await act(async () => {})
  expect(metalens.undo).not.toHaveBeenCalled()
  editable.remove(); area.remove()

  // macOS: the menu accelerator fires while a field has focus -> text undo, not file undo.
  const execCommand = vi.fn().mockReturnValue(true)
  document.execCommand = execCommand
  await act(async () => menuUndo({ keyboard: true }))
  expect(execCommand).toHaveBeenCalledWith('undo')
  expect(metalens.undo).not.toHaveBeenCalled()
  delete document.execCommand

  input.blur()
  const outside = fireEvent.keyDown(document.body, { key: 'z', ctrlKey: true })
  expect(outside).toBe(false)   // consumed by the file undo
  await screen.findByText(/Undone: Edit Title/)
  expect(metalens.undo).toHaveBeenCalledExactlyOnceWith('tok-1')
})

test('Edit > Undo clicked from the menu undoes the file even with a focused field; Ctrl+Shift+Z is not undo', async () => {
  const undo = await appWithEdits(2)
  metalens.undo.mockResolvedValue({ ok: true, errors: [], keys: ['pdf:Title'], undo_token: null })
  fireEvent.keyDown(document.body, { key: 'z', ctrlKey: true, shiftKey: true })
  await act(async () => {})
  expect(metalens.undo).not.toHaveBeenCalled()
  screen.getByLabelText('Title').focus()
  await act(async () => menuUndo({ keyboard: false }))
  await screen.findByText(/Undone: Edit Title/)
  expect(metalens.undo).toHaveBeenCalledExactlyOnceWith('tok-2')
  expect(undo.disabled).toBe(false)
})
