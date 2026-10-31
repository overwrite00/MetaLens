import { useCallback, useEffect, useRef, useState } from 'react'

const DEFAULT_SETTINGS = { autoCheckUpdates: false, ignoredVersion: null }

// Update state for the whole app. The network work happens in the Electron main
// process; this hook only drives it through window.electronAPI. Statuses:
// idle | checking | current | available | downloading | error
export function useUpdates() {
  const [settings, setSettings] = useState(DEFAULT_SETTINGS)
  const [status, setStatus]     = useState('idle')
  const [update, setUpdate]     = useState(null)
  const [error, setError]       = useState(null)
  const [progress, setProgress] = useState(null)
  const alive = useRef(true)

  useEffect(() => {
    alive.current = true
    return () => { alive.current = false }
  }, [])

  const check = useCallback(async ({ auto = false } = {}) => {
    const api = window.electronAPI
    if (!api?.checkForUpdates) return
    setStatus('checking'); setError(null)
    let result
    try {
      result = await api.checkForUpdates({ auto })
    } catch (e) {
      result = { error: e.message }
    }
    if (!alive.current) return
    if (result?.skipped) setStatus('idle')
    else if (result?.error) {
      // A failed startup check stays silent; only a user-requested one reports it.
      if (auto) setStatus('idle')
      else { setStatus('error'); setError(result.error) }
    } else {
      setUpdate(result?.update ?? null)
      setStatus(result?.update ? 'available' : 'current')
    }
  }, [])

  useEffect(() => {
    const api = window.electronAPI
    if (!api?.getUpdateSettings) return
    api.getUpdateSettings().then(stored => {
      if (!alive.current) return
      setSettings(stored)
      if (stored.autoCheckUpdates) check({ auto: true })
    }).catch(() => {})
  }, [check])

  useEffect(() => {
    const unsubscribe = window.electronAPI?.onUpdateProgress?.(setProgress)
    return () => { if (typeof unsubscribe === 'function') unsubscribe() }
  }, [])

  const saveSettings = useCallback(async patch => {
    try {
      const saved = await window.electronAPI.setUpdateSettings(patch)
      if (alive.current) { setSettings(saved); return saved }
    } catch (e) {
      if (alive.current) { setStatus('error'); setError(e.message) }
    }
    return null
  }, [])

  const setAutoCheck = useCallback(enabled => saveSettings({ autoCheckUpdates: enabled }), [saveSettings])

  const setIgnored = useCallback(async ignored => {
    if (!update) return
    const saved = await saveSettings({ ignoredVersion: ignored ? update.version : null })
    if (saved) setUpdate(current => current && { ...current, ignored })
  }, [update, saveSettings])

  const install = useCallback(async () => {
    setStatus('downloading'); setError(null); setProgress(null)
    let result
    try {
      result = await window.electronAPI.installUpdate()
    } catch (e) {
      result = { error: e.message }
    }
    if (!alive.current || result?.ok) return
    setStatus(result?.error === 'Download cancelled' ? 'available' : 'error')
    if (result?.error !== 'Download cancelled') setError(result?.error ?? 'Installation failed')
  }, [])

  const cancel = useCallback(() => window.electronAPI?.cancelUpdate?.(), [])
  const openRelease = useCallback(() => window.electronAPI?.openReleasePage?.(), [])

  return {
    supported: Boolean(window.electronAPI?.checkForUpdates),
    settings, status, update, error, progress,
    notice: status === 'available' && Boolean(update) && !update.ignored,
    check, setAutoCheck, ignore: () => setIgnored(true), unignore: () => setIgnored(false),
    install, cancel, openRelease,
  }
}
