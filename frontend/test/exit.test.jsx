import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import App from '../src/App'

vi.mock('../src/api/client', () => ({ metalens: {} }))
vi.mock('../src/components/FolderPanel', () => ({ FolderPanel: () => null }))
vi.mock('../src/components/FilePanel', () => ({ FilePanel: () => null }))
vi.mock('../src/components/DetailPanel', () => ({ DetailPanel: () => null }))

let api

beforeEach(() => {
  api = {
    onMenuOpenFolder: vi.fn(), onMenuUndo: vi.fn(), onMenuAbout: vi.fn(), removeAllListeners: vi.fn(),
    onMenuCheckUpdates: vi.fn(() => vi.fn()), onUpdateProgress: vi.fn(() => vi.fn()),
    getAppVersion: vi.fn().mockResolvedValue('0.5.0'),
    getUpdateSettings: vi.fn(async () => ({ autoCheckUpdates: false, ignoredVersion: null })),
    quitApp: vi.fn().mockResolvedValue(undefined),
  }
  window.electronAPI = api
})
afterEach(() => { delete window.electronAPI })

test('the Exit button asks the main process to quit, once', async () => {
  render(<App />)
  const button = screen.getByRole('button', { name: /exit/i })
  fireEvent.click(button)
  fireEvent.click(button)
  await waitFor(() => expect(api.quitApp).toHaveBeenCalledOnce())
  expect(button.disabled).toBe(true)
})

test('the Exit button is enabled again when quitting fails', async () => {
  api.quitApp.mockRejectedValueOnce(new Error('boom'))
  render(<App />)
  const button = screen.getByRole('button', { name: /exit/i })
  fireEvent.click(button)
  await waitFor(() => expect(button.disabled).toBe(false))
})

test('there is no Exit button outside Electron', () => {
  delete window.electronAPI
  render(<App />)
  expect(screen.queryByRole('button', { name: /exit/i })).toBeNull()
})
