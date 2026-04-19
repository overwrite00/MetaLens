import { useState, useEffect, useCallback } from 'react'
import { File, Image, Music, Video, FileText, Archive, AlertCircle } from 'lucide-react'
import { Spinner } from './ui/Spinner'
import { metalens } from '../api/client'

const HANDLER_ICONS = {
  image: Image, audio: Music, video: Video,
  pdf: FileText, office: FileText, ole: FileText,
}

function getIcon(handler) {
  return HANDLER_ICONS[handler] || File
}

function formatSize(bytes) {
  if (bytes == null) return '—'
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
  return (bytes / 1024 / 1024).toFixed(2) + ' MB'
}

function formatDate(ts) {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleString()
}

export function FilePanel({ currentPath, onSelectFile, onSelectTwo }) {
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [selected, setSelected] = useState(new Set())
  const [sortKey, setSortKey] = useState('name')
  const [sortAsc, setSortAsc] = useState(true)

  useEffect(() => {
    if (!currentPath) return
    loadDir(currentPath)
  }, [currentPath])

  async function loadDir(path) {
    setLoading(true)
    setError(null)
    setSelected(new Set())
    try {
      const data = await metalens.list(path)
      setItems(data.items.filter(i => !i.is_dir))
    } catch (e) {
      setError(e.message)
      setItems([])
    } finally {
      setLoading(false)
    }
  }

  function toggleSort(key) {
    if (sortKey === key) setSortAsc(a => !a)
    else { setSortKey(key); setSortAsc(true) }
  }

  const sorted = [...items].sort((a, b) => {
    let va = a[sortKey] ?? '', vb = b[sortKey] ?? ''
    if (typeof va === 'string') va = va.toLowerCase()
    if (typeof vb === 'string') vb = vb.toLowerCase()
    return sortAsc ? (va > vb ? 1 : -1) : (va < vb ? 1 : -1)
  })

  function handleClick(item, e) {
    const path = item.path
    setSelected(prev => {
      const next = new Set(e.shiftKey || e.ctrlKey || e.metaKey ? prev : [])
      if (next.has(path)) next.delete(path)
      else next.add(path)
      const arr = [...next]
      if (arr.length === 1) onSelectFile(arr[0])
      else if (arr.length === 2) onSelectTwo(arr[0], arr[1])
      else if (arr.length === 0) onSelectFile(null)
      return next
    })
  }

  const SortIcon = ({ col }) => sortKey === col
    ? <span className="text-cyber-cyan ml-0.5">{sortAsc ? '↑' : '↓'}</span>
    : null

  return (
    <div className="flex flex-col h-full bg-cyber-bg">
      {/* Header */}
      <div className="flex items-center bg-cyber-panel border-b border-cyber-border text-[10px] font-mono text-cyber-muted select-none">
        <div className="w-8 flex-shrink-0 px-2 py-2" />
        <ColHeader label="Name"     col="name"    onClick={toggleSort} sortKey={sortKey} sortAsc={sortAsc} flex="flex-1 min-w-0" />
        <ColHeader label="Type"     col="handler"  onClick={toggleSort} sortKey={sortKey} sortAsc={sortAsc} width="w-20" />
        <ColHeader label="Size"     col="size"     onClick={toggleSort} sortKey={sortKey} sortAsc={sortAsc} width="w-24" />
        <ColHeader label="Modified" col="mtime"    onClick={toggleSort} sortKey={sortKey} sortAsc={sortAsc} width="w-36" />
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto">
        {loading && (
          <div className="flex items-center justify-center h-24 gap-2 text-cyber-muted text-xs">
            <Spinner size={14} /> Loading…
          </div>
        )}
        {error && (
          <div className="flex items-center gap-2 px-4 py-3 text-cyber-danger text-xs">
            <AlertCircle size={14} /> {error}
          </div>
        )}
        {!loading && !error && !currentPath && (
          <div className="flex items-center justify-center h-full text-cyber-dim text-xs">
            Select a folder to browse files
          </div>
        )}
        {!loading && !error && currentPath && sorted.length === 0 && (
          <div className="flex items-center justify-center h-24 text-cyber-dim text-xs">
            No files in this folder
          </div>
        )}
        {sorted.map(item => {
          const Icon = getIcon(item.handler)
          const isSel = selected.has(item.path)
          return (
            <div key={item.path}
              onClick={e => handleClick(item, e)}
              className={`flex items-center cursor-pointer select-none transition-colors
                border-b border-cyber-border/30
                ${isSel
                  ? 'bg-cyber-cyan/10 border-l-2 border-l-cyber-cyan'
                  : 'hover:bg-cyber-tertiary border-l-2 border-l-transparent'}`}
            >
              <div className="w-8 flex-shrink-0 flex items-center justify-center">
                <Icon size={13}
                  className={isSel ? 'text-cyber-cyan' : 'text-cyber-muted'} />
              </div>
              <div className="flex-1 min-w-0 py-1.5 pr-2 font-mono text-xs truncate"
                title={item.name}>
                {item.name}
              </div>
              <div className="w-20 text-[10px] text-cyber-muted font-mono uppercase truncate">
                {item.ext?.replace('.', '') || item.handler || '—'}
              </div>
              <div className="w-24 text-[10px] text-cyber-muted font-mono text-right pr-3">
                {formatSize(item.size)}
              </div>
              <div className="w-36 text-[10px] text-cyber-muted pr-2 truncate">
                {formatDate(item.mtime)}
              </div>
            </div>
          )
        })}
      </div>

      {/* Footer */}
      <div className="px-3 py-1 border-t border-cyber-border text-[10px] text-cyber-dim font-mono">
        {sorted.length} file{sorted.length !== 1 ? 's' : ''}
        {selected.size > 0 && ` · ${selected.size} selected`}
        {selected.size === 2 && <span className="text-cyber-cyan ml-1">— ready to compare</span>}
      </div>
    </div>
  )
}

function ColHeader({ label, col, onClick, sortKey, sortAsc, flex, width }) {
  const active = sortKey === col
  return (
    <button
      onClick={() => onClick(col)}
      className={`${flex ?? ''} ${width ?? ''} px-2 py-2 text-left hover:text-cyber-text transition-colors
        ${active ? 'text-cyber-cyan' : ''}`}
    >
      {label}
      {active && <span className="ml-0.5">{sortAsc ? '↑' : '↓'}</span>}
    </button>
  )
}
