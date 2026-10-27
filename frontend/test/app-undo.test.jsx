import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import App from '../src/App'
import { metalens } from '../src/api/client'

vi.mock('../src/api/client', () => ({ metalens: { write: vi.fn() } }))
vi.mock('../src/components/FolderPanel', () => ({ FolderPanel: ({ onSelectPath }) => <button onClick={() => onSelectPath('folder')}>Folder</button> }))
vi.mock('../src/components/FilePanel', () => ({ FilePanel: ({ onSelectFile }) => <button onClick={() => onSelectFile('a.pdf')}>Select file</button> }))
vi.mock('../src/components/DetailPanel', () => ({ DetailPanel: ({ filePath, onUndoPush, reloadKey, runExclusive }) => <>
  <span>File: {filePath || 'none'}</span><span>Revision: {reloadKey}</span>
  <button onClick={() => runExclusive(async () => onUndoPush({ filePath, fields: [{ key: 'pdf:Title', value: 'original' }] }))}>Edit file</button>
</> }))

test('toolbar reacts to edits, menu undo refreshes current selection, and failed undo remains retryable', async () => {
  let menuUndo
  window.electronAPI = { onMenuOpenFolder: vi.fn(), onMenuUndo: cb => { menuUndo = cb }, onMenuAbout: vi.fn(), removeAllListeners: vi.fn() }
  metalens.write.mockRejectedValueOnce(new Error('access denied')).mockResolvedValueOnce({})
  render(<App />)
  const undo = screen.getByRole('button', { name: 'Undo' })
  expect(undo.disabled).toBe(true)
  fireEvent.click(screen.getByText('Select file')); fireEvent.click(screen.getByText('Edit file'))
  await waitFor(() => expect(undo.disabled).toBe(false))
  await act(async () => menuUndo())
  await screen.findByText('Undo failed: access denied'); expect(undo.disabled).toBe(false)
  await act(async () => menuUndo())
  await screen.findByText('Undo applied'); expect(screen.getByText('Revision: 1')).toBeTruthy()
  expect(undo.disabled).toBe(true)
  expect(metalens.write).toHaveBeenLastCalledWith('a.pdf', [{ key: 'pdf:Title', value: 'original' }])
  fireEvent.click(screen.getByText('Folder')); expect(screen.getByText('File: none')).toBeTruthy()
})
