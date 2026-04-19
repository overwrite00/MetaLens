import { useState, useEffect, useCallback, useRef } from 'react'
import { FolderPanel } from './components/FolderPanel'
import { FilePanel } from './components/FilePanel'
import { DetailPanel } from './components/DetailPanel'
import { AboutDialog } from './components/AboutDialog'
import { Button } from './components/ui/Button'
import { useUndoStack } from './hooks/useUndoStack'
import { metalens } from './api/client'
import { FolderOpen, RotateCcw, GitCompare, Layers } from 'lucide-react'
import './styles/globals.css'

export default function App() {
  const [currentPath, setCurrentPath]   = useState(null)
  const [selectedFile, setSelectedFile] = useState(null)
  const [fileB, setFileB]               = useState(null)
  const [showAbout, setShowAbout]       = useState(false)
  const [undoMsg, setUndoMsg]           = useState(null)
  const { push: undoPush, pop: undoPop, canUndo } = useUndoStack()

  // Splitter widths (px)
  const [leftW, setLeftW]   = useState(220)
  const [rightW, setRightW] = useState(360)
  const dragging = useRef(null)

  // ── Menu events from Electron ─────────────────────────────────────────────
  useEffect(() => {
    if (!window.electronAPI) return
    const openFolder = () => openFolderDialog()
    const undo       = () => handleUndo()
    const about      = () => setShowAbout(true)
    window.electronAPI.onMenuOpenFolder(openFolder)
    window.electronAPI.onMenuUndo(undo)
    window.electronAPI.onMenuAbout(about)
    return () => {
      window.electronAPI.removeAllListeners('menu-open-folder')
      window.electronAPI.removeAllListeners('menu-undo')
      window.electronAPI.removeAllListeners('menu-about')
    }
  }, [])

  async function openFolderDialog() {
    if (!window.electronAPI) return
    const path = await window.electronAPI.openFolderDialog()
    if (path) setCurrentPath(path)
  }

  // ── Undo ──────────────────────────────────────────────────────────────────
  async function handleUndo() {
    if (!canUndo()) return
    const entry = undoPop()
    if (!entry) return
    try {
      await metalens.write(entry.filePath, entry.fields)
      setUndoMsg('Undo applied')
      setTimeout(() => setUndoMsg(null), 2000)
      if (selectedFile === entry.filePath) {
        // Trigger DetailPanel reload by toggling selectedFile
        setSelectedFile(null)
        requestAnimationFrame(() => setSelectedFile(entry.filePath))
      }
    } catch (e) {
      setUndoMsg(`Undo failed: ${e.message}`)
      setTimeout(() => setUndoMsg(null), 3000)
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
    if (path !== fileB) setFileB(null)
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
            disabled={!canUndo()} title="Undo last write">
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

        <div className="ml-auto" style={{ WebkitAppRegion: 'no-drag' }}>
          <Button variant="ghost" size="xs" onClick={() => setShowAbout(true)}>About</Button>
        </div>
      </div>

      {/* ── Three-panel layout ── */}
      <div className="flex flex-1 min-h-0 overflow-hidden">
        {/* Left: Folder tree */}
        <div style={{ width: leftW, flexShrink: 0 }} className="min-h-0 overflow-hidden">
          <FolderPanel onSelectPath={setCurrentPath} />
        </div>

        {/* Resize handle L */}
        <div className="resize-handle" onMouseDown={() => onMouseDown('left')} />

        {/* Center: File list */}
        <div className="flex-1 min-w-0 min-h-0 overflow-hidden">
          <FilePanel
            currentPath={currentPath}
            onSelectFile={handleSelectFile}
            onSelectTwo={handleSelectTwo}
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
          />
        </div>
      </div>

      {/* About dialog */}
      {showAbout && <AboutDialog onClose={() => setShowAbout(false)} />}
    </div>
  )
}
