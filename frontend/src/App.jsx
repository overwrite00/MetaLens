import { useState, useEffect, useRef } from 'react'
import { FolderPanel } from './components/FolderPanel'
import { FilePanel } from './components/FilePanel'
import { DetailPanel } from './components/DetailPanel'
import { AboutDialog } from './components/AboutDialog'
import { Button } from './components/ui/Button'
import { useUndoStack } from './hooks/useUndoStack'
import { useUpdates } from './hooks/useUpdates'
import { metalens } from './api/client'
import { isTextEditable, isUndoShortcut } from './lib/keyboard'
import { FolderOpen, RotateCcw, GitCompare, Layers, CircleArrowUp } from 'lucide-react'
import './styles/globals.css'

const baseName = path => (path ? String(path).split(/[\\/]/).pop() : 'the file')

export default function App() {
  const [currentPath, setCurrentPath]   = useState(null)
  const [selectedFile, setSelectedFile] = useState(null)
  const [fileB, setFileB]               = useState(null)
  const [showAbout, setShowAbout]       = useState(false)
  const [undoMsg, setUndoMsg]           = useState(null)
  const { push: undoPush, undo, canUndo, runExclusive, busy } = useUndoStack()
  const updates = useUpdates()
  const { check: checkUpdates } = updates
  const [reloadKey, setReloadKey] = useState(0)
  const [filesReloadKey, setFilesReloadKey] = useState(0)

  // Splitter widths (px)
  const [leftW, setLeftW]   = useState(220)
  const [rightW, setRightW] = useState(360)
  const dragging = useRef(null)
  const undoMsgTimer = useRef(null)
  const handleUndoRef = useRef(null)
  handleUndoRef.current = handleUndo

  // ── Menu events from Electron ─────────────────────────────────────────────
  useEffect(() => {
    const api = window.electronAPI
    if (!api) return
    const openFolder = () => openFolderDialog()
    // `keyboard`: the menu accelerator fired (macOS, where it cannot be left to the
    // page). Inside a text field that is the field's own undo, not the file undo (D11).
    const undo       = payload => {
      if (payload?.keyboard && isTextEditable(document.activeElement)) {
        document.execCommand?.('undo')
        return
      }
      handleUndoRef.current()
    }
    const about      = () => setShowAbout(true)
    api.onMenuOpenFolder(openFolder)
    api.onMenuUndo(undo)
    api.onMenuAbout(about)
    return () => {
      api.removeAllListeners('menu-open-folder')
      api.removeAllListeners('menu-undo')
      api.removeAllListeners('menu-about')
    }
  }, [])

  // Ctrl+Z (Windows/Linux: the menu shows it but leaves it to the page). In a text
  // field the event is left alone so the browser undoes the typing; elsewhere it
  // undoes the last save.
  useEffect(() => {
    function onKeyDown(event) {
      if (!isUndoShortcut(event) || event.defaultPrevented || event.repeat) return
      if (isTextEditable(event.target)) return
      event.preventDefault()
      handleUndoRef.current()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  useEffect(() => {
    const unsubscribe = window.electronAPI?.onMenuCheckUpdates?.(() => {
      setShowAbout(true)
      checkUpdates()
    })
    return () => { if (typeof unsubscribe === 'function') unsubscribe() }
  }, [checkUpdates])

  async function openFolderDialog() {
    if (!window.electronAPI) return
    const path = await window.electronAPI.openFolderDialog()
    if (path) handleSelectPath(path)
  }

  // ── Undo ──────────────────────────────────────────────────────────────────
  function showUndoMsg(message, ms) {
    clearTimeout(undoMsgTimer.current)
    setUndoMsg(message)
    undoMsgTimer.current = setTimeout(() => setUndoMsg(null), ms)
  }
  useEffect(() => () => clearTimeout(undoMsgTimer.current), [])

  const refreshViews = () => {
    // Refresh the record and the file list even if the selection changed meanwhile.
    setReloadKey(value => value + 1)
    setFilesReloadKey(value => value + 1)
  }

  async function handleUndo() {
    try {
      let result = null
      const entry = await undo(async item => { result = await metalens.undo(item.token) })
      if (!entry) return
      // Non-blocking server warnings (e.g. the file owner could not be restored) stay visible longer.
      const warnings = Array.isArray(result?.warnings) ? result.warnings.filter(Boolean) : []
      showUndoMsg(`Undone: ${entry.label || 'last save'} (${baseName(entry.filePath)})${warnings.length ? ` — ${warnings.join('; ')}` : ''}`,
        warnings.length ? 6000 : 2500)
      refreshViews()
    } catch (e) {
      const name = baseName(e.undoEntry?.filePath)
      if (e.undoDiscarded === 'changed') {
        showUndoMsg(`Undo not possible: ${name} changed since the save (entry removed)`, 6000)
        refreshViews()
      } else if (e.undoDiscarded === 'expired') {
        showUndoMsg(`Undo no longer available for ${name} (entry removed)`, 6000)
      } else {
        showUndoMsg(`Undo failed: ${e.message}`, 4000)
      }
    }
  }

  // ── Splitter drag ─────────────────────────────────────────────────────────
  function onMouseDown(side) {
    dragging.current = side
  }
  useEffect(() => {
    function onMove(e) {
      if (!dragging.current) return
      if (dragging.current === 'left')  setLeftW(w => Math.max(140, Math.min(400, w + e.movementX)))
      if (dragging.current === 'right') setRightW(w => Math.max(240, Math.min(600, w - e.movementX)))
    }
    function onUp() { dragging.current = null }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    return () => { window.removeEventListener('mousemove', onMove); window.removeEventListener('mouseup', onUp) }
  }, [])

  function handleSelectTwo(a, b) {
    setSelectedFile(a)
    setFileB(b)
  }

  function handleSelectFile(path) {
    setSelectedFile(path)
    setFileB(null)
  }

  function handleSelectPath(path) {
    setCurrentPath(path)
    setSelectedFile(null)
    setFileB(null)
  }

  return (
    <div className="flex flex-col h-screen overflow-hidden bg-cyber-bg text-cyber-text">
      {/* ── Title bar / toolbar ── */}
      <div className="flex items-center gap-2 px-3 py-1.5 bg-cyber-panel border-b border-cyber-border select-none
        drag" style={{ WebkitAppRegion: 'drag' }}>
        <div className="flex items-center gap-1.5 mr-3" style={{ WebkitAppRegion: 'no-drag' }}>
          <Layers size={16} className="text-cyber-cyan" />
          <span className="font-mono text-xs text-cyber-cyan font-semibold tracking-wide">MetaLens</span>
        </div>

        {/* Toolbar buttons */}
        <div className="flex items-center gap-1" style={{ WebkitAppRegion: 'no-drag' }}>
          <Button variant="ghost" size="xs" onClick={openFolderDialog} title="Open Folder (Ctrl+O)">
            <FolderOpen size={12} /> Open Folder
          </Button>
          <Button variant="ghost" size="xs" onClick={handleUndo}
            disabled={!canUndo() || busy} title="Undo last save (Ctrl+Z)">
            <RotateCcw size={12} /> Undo
          </Button>
          {fileB && (
            <Button variant="primary" size="xs"
              onClick={() => { setShowAbout(false); /* already in diff via DetailPanel */ }}>
              <GitCompare size={12} /> Comparing 2 files
            </Button>
          )}
        </div>

        {undoMsg && (
          <span className="ml-3 text-[10px] font-mono text-cyber-warning">{undoMsg}</span>
        )}

        <div className="ml-auto flex items-center gap-1" style={{ WebkitAppRegion: 'no-drag' }}>
          {updates.notice && (
            <Button variant="primary" size="xs" onClick={() => setShowAbout(true)}
              title="A new version is available">
              <CircleArrowUp size={12} /> Update v{updates.update.version}
            </Button>
          )}
          <Button variant="ghost" size="xs" onClick={() => setShowAbout(true)}>About</Button>
        </div>
      </div>

      {/* ── Three-panel layout ── */}
      <div className="flex flex-1 min-h-0 overflow-hidden">
        {/* Left: Folder tree */}
        <div style={{ width: leftW, flexShrink: 0 }} className="min-h-0 overflow-hidden">
          <FolderPanel onSelectPath={handleSelectPath}
            onRefresh={() => setFilesReloadKey(value => value + 1)} />
        </div>

        {/* Resize handle L */}
        <div className="resize-handle" onMouseDown={() => onMouseDown('left')} />

        {/* Center: File list */}
        <div className="flex-1 min-w-0 min-h-0 overflow-hidden">
          <FilePanel
            currentPath={currentPath}
            reloadKey={filesReloadKey}
            onSelectFile={handleSelectFile}
            onSelectTwo={handleSelectTwo}
            onUndoPush={undoPush}
            runExclusive={runExclusive}
            onChanged={() => setFilesReloadKey(value => value + 1)}
          />
        </div>

        {/* Resize handle R */}
        <div className="resize-handle" onMouseDown={() => onMouseDown('right')} />

        {/* Right: Detail panel */}
        <div style={{ width: rightW, flexShrink: 0 }} className="min-h-0 overflow-hidden">
          <DetailPanel
            filePath={selectedFile}
            filePathB={fileB}
            onUndoPush={undoPush}
            runExclusive={runExclusive}
            mutationBusy={busy}
            reloadKey={reloadKey}
          />
        </div>
      </div>

      {/* About dialog */}
      {showAbout && <AboutDialog updates={updates} onClose={() => setShowAbout(false)} />}
    </div>
  )
}
