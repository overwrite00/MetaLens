import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import App from '../src/App'

vi.mock('../src/api/client', () => ({ metalens: {} }))
vi.mock('../src/components/FolderPanel', () => ({ FolderPanel: () => null }))
vi.mock('../src/components/FilePanel', () => ({ FilePanel: () => null }))
vi.mock('../src/components/DetailPanel', () => ({ DetailPanel: () => null }))

const UPDATE = { version: '0.4.0', channel: 'stable', notes: 'Fixed things', canInstall: true, ignored: false }
let api, menuCheck, progress

beforeEach(() => {
  menuCheck = progress = undefined
  const settings = { autoCheckUpdates: false, ignoredVersion: null }
  api = {
    onMenuOpenFolder: vi.fn(), onMenuUndo: vi.fn(), onMenuAbout: vi.fn(), removeAllListeners: vi.fn(),
    onMenuCheckUpdates: cb => { menuCheck = cb; return vi.fn() },
    onUpdateProgress: cb => { progress = cb; return vi.fn() },
    getAppVersion: vi.fn().mockResolvedValue('0.3.6'),
    getUpdateSettings: vi.fn(async () => ({ ...settings })),
    setUpdateSettings: vi.fn(async patch => Object.assign(settings, patch) && { ...settings }),
    checkForUpdates: vi.fn().mockResolvedValue({ currentVersion: '0.3.6', update: { ...UPDATE } }),
    installUpdate: vi.fn(), cancelUpdate: vi.fn(), openReleasePage: vi.fn(), openRepo: vi.fn(),
  }
  window.electronAPI = api
})
afterEach(() => { delete window.electronAPI })

const openAbout = () => fireEvent.click(screen.getByRole('button', { name: 'About' }))

test('About links to the repository through the main process', async () => {
  render(<App />)
  openAbout()
  const link = await screen.findByRole('link', { name: /github\.com\/overwrite00\/MetaLens/ })
  expect(link.getAttribute('href')).toBe('https://github.com/overwrite00/MetaLens')
  fireEvent.click(link)
  expect(api.openRepo).toHaveBeenCalledOnce()
})

test('nothing is checked at startup unless the user opted in', async () => {
  render(<App />)
  await waitFor(() => expect(api.getUpdateSettings).toHaveBeenCalled())
  expect(api.checkForUpdates).not.toHaveBeenCalled()
  expect(screen.queryByText(/Update v/)).toBeNull()
})

test('opted-in startup check shows a toolbar notice unless the version is ignored', async () => {
  api.getUpdateSettings.mockResolvedValue({ autoCheckUpdates: true, ignoredVersion: null })
  const { unmount } = render(<App />)
  await screen.findByText('Update v0.4.0')
  expect(api.checkForUpdates).toHaveBeenCalledWith({ auto: true })
  unmount()

  api.checkForUpdates.mockResolvedValue({ update: { ...UPDATE, ignored: true } })
  render(<App />)
  await waitFor(() => expect(api.checkForUpdates).toHaveBeenCalledTimes(2))
  expect(screen.queryByText(/Update v/)).toBeNull()
})

test('a failed startup check stays silent', async () => {
  api.getUpdateSettings.mockResolvedValue({ autoCheckUpdates: true, ignoredVersion: null })
  api.checkForUpdates.mockResolvedValue({ error: 'offline' })
  render(<App />)
  await waitFor(() => expect(api.checkForUpdates).toHaveBeenCalled())
  openAbout()
  await screen.findByRole('dialog')
  expect(screen.queryByRole('alert')).toBeNull()
})

test('manual check reports an available beta with its notes and a failed check as an error', async () => {
  api.checkForUpdates.mockResolvedValueOnce({ update: { ...UPDATE, channel: 'beta', version: '0.4.0-beta.1' } })
    .mockResolvedValueOnce({ error: 'GitHub rate limit reached, try again later' })
    .mockResolvedValueOnce({ update: null })
  render(<App />)
  openAbout()
  fireEvent.click(await screen.findByRole('button', { name: /Check for updates/ }))
  await screen.findByText('v0.4.0-beta.1')
  expect(screen.getByText(/New beta available/)).toBeTruthy()
  expect(screen.getByText('Fixed things')).toBeTruthy()
  expect(api.checkForUpdates).toHaveBeenCalledWith({ auto: false })

  fireEvent.click(screen.getByRole('button', { name: /Check for updates/ }))
  expect((await screen.findByRole('alert')).textContent).toMatch(/rate limit/)

  fireEvent.click(screen.getByRole('button', { name: /Check for updates/ }))
  await screen.findByText(/latest version/)
  expect(screen.queryByRole('alert')).toBeNull()
})

test('Help menu check opens About and runs the check', async () => {
  render(<App />)
  await act(async () => menuCheck())
  await screen.findByText('v0.4.0')
  expect(screen.getByRole('dialog')).toBeTruthy()
})

test('ignoring a version and the auto-check option are persisted through the main process', async () => {
  render(<App />)
  openAbout()
  fireEvent.click(await screen.findByRole('button', { name: /Check for updates/ }))
  fireEvent.click(await screen.findByRole('button', { name: 'Ignore this version' }))
  await screen.findByText('(ignored)')
  expect(api.setUpdateSettings).toHaveBeenCalledWith({ ignoredVersion: '0.4.0' })
  expect(screen.queryByText('Update v0.4.0')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Stop ignoring' }))
  await waitFor(() => expect(api.setUpdateSettings).toHaveBeenLastCalledWith({ ignoredVersion: null }))

  const box = screen.getByRole('checkbox')
  expect(box.checked).toBe(false)
  fireEvent.click(box)
  await waitFor(() => expect(box.checked).toBe(true))
  expect(api.setUpdateSettings).toHaveBeenLastCalledWith({ autoCheckUpdates: true })
})

test('install shows download progress, can be cancelled, and reports failures', async () => {
  let finish
  api.installUpdate.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  render(<App />)
  openAbout()
  fireEvent.click(await screen.findByRole('button', { name: /Check for updates/ }))
  fireEvent.click(await screen.findByRole('button', { name: /Download & install/ }))
  await screen.findByText(/Starting download/)
  act(() => progress({ received: 50, total: 200 }))
  await screen.findByText('Downloading… 25%')
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
  expect(api.cancelUpdate).toHaveBeenCalledOnce()
  await act(async () => finish({ error: 'Download cancelled' }))
  await screen.findByRole('button', { name: /Download & install/ })
  expect(screen.queryByRole('alert')).toBeNull()

  api.installUpdate.mockResolvedValueOnce({ error: 'Checksum mismatch: the download was discarded' })
  fireEvent.click(screen.getByRole('button', { name: /Download & install/ }))
  expect((await screen.findByRole('alert')).textContent).toMatch(/Checksum mismatch/)
  expect(screen.getByRole('button', { name: /Download & install/ })).toBeTruthy()
})

test('builds that cannot install only offer the release page', async () => {
  api.checkForUpdates.mockResolvedValue({ update: { ...UPDATE, canInstall: false } })
  render(<App />)
  openAbout()
  fireEvent.click(await screen.findByRole('button', { name: /Check for updates/ }))
  fireEvent.click(await screen.findByRole('button', { name: /Release page/ }))
  expect(api.openReleasePage).toHaveBeenCalledOnce()
  expect(screen.queryByRole('button', { name: /Download & install/ })).toBeNull()
})

test('Escape closes About', async () => {
  render(<App />)
  openAbout()
  await screen.findByRole('dialog')
  fireEvent.keyDown(window, { key: 'Escape' })
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
})

test('without Electron the update section is hidden and nothing throws', async () => {
  delete window.electronAPI
  render(<App />)
  openAbout()
  await screen.findByText('v?')
  expect(screen.queryByText('Updates')).toBeNull()
  expect(screen.getByRole('link', { name: /github\.com/ })).toBeTruthy()
})
