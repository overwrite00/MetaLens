import { useState, useEffect, useRef } from 'react'
import { Folder, FolderOpen, HardDrive, Home, ChevronRight, ChevronDown, RefreshCw } from 'lucide-react'
import { Button } from './ui/Button'
import { metalens } from '../api/client'

export function FolderPanel({ onSelectPath }) {
  const [tree, setTree] = useState([])
  const [expanded, setExpanded] = useState({})
  const [selected, setSelected] = useState(null)
  const [bookmarks, setBookmarks] = useState([])
  const [error, setError] = useState(null)
  const generation = useRef(0)
  const requests = useRef(new Set())

  useEffect(() => {
    initRoots()
    return () => { generation.current += 1 }
  }, [])

  async function initRoots() {
    const version = ++generation.current
    requests.current.clear()
    setExpanded({}); setTree([]); setSelected(null); setError(null)
    const roots = []
    try {
      if (window.electronAPI) {
        const drives = await window.electronAPI.listDrives()
        const home = await window.electronAPI.homeDir()
        if (drives.length) {
          drives.forEach(d => roots.push({ name: d, path: d, type: 'drive' }))
        } else {
          roots.push({ name: 'Root', path: '/', type: 'drive' })
        }
        if (version !== generation.current) return
        if (home) setBookmarks([{ label: 'Home', path: home, icon: Home }])
      }
      setTree(roots.map(r => ({ ...r, children: null, loading: false })))
    } catch (e) {
      if (version === generation.current) setError(e.message)
    }
  }

  async function expand(node) {
    const key = node.path
    if (expanded[key]) {
      setExpanded(e => ({ ...e, [key]: false }))
      return
    }
    if (requests.current.has(key)) return
    if (!node.children && !await loadChildren(node)) return
    setExpanded(e => ({ ...e, [key]: true }))
  }

  async function loadChildren(node) {
    const version = generation.current
    requests.current.add(node.path)
    setError(null)
    setTree(prev => updateNode(prev, node.path, n => ({ ...n, loading: true })))
    try {
      const data = await metalens.list(node.path)
      if (version !== generation.current) return false
      const dirs = (data.items || []).filter(i => i.is_dir)
        .map(i => ({ name: i.name, path: i.path, type: 'dir', children: null, loading: false }))
      setTree(prev => updateNode(prev, node.path, n => ({ ...n, children: dirs, loading: false })))
      return true
    } catch (e) {
      if (version === generation.current) {
        setError(e.message)
        setTree(prev => updateNode(prev, node.path, n => ({ ...n, loading: false })))
      }
      return false
    } finally {
      if (version === generation.current) requests.current.delete(node.path)
    }
  }

  function select(path) {
    setSelected(path)
    onSelectPath(path)
  }

  return (
    <div className="flex flex-col h-full bg-cyber-panel border-r border-cyber-border">
      {/* Bookmarks */}
      <div className="px-2 py-2 border-b border-cyber-border">
        <div className="text-[10px] text-cyber-muted font-mono uppercase tracking-wider mb-1 px-1">Bookmarks</div>
        {bookmarks.map(bm => (
          <button key={bm.path}
            onClick={() => select(bm.path)}
            className={`flex items-center gap-1.5 w-full px-2 py-1 rounded text-xs text-left transition-colors
              ${selected === bm.path ? 'bg-cyber-cyan/10 text-cyber-cyan' : 'text-cyber-muted hover:text-cyber-text hover:bg-cyber-tertiary'}`}>
            <bm.icon size={12} />
            {bm.label}
          </button>
        ))}
      </div>

      {/* Tree */}
      {error && <div role="alert" className="px-3 py-2 text-cyber-danger text-xs">{error}</div>}
      <div className="flex-1 overflow-y-auto px-1 py-1">
        {tree.map(node => (
          <TreeNode key={node.path} node={node} depth={0}
            expanded={expanded} onExpand={expand}
            selected={selected} onSelect={select} />
        ))}
      </div>

      {/* Refresh */}
      <div className="p-2 border-t border-cyber-border">
        <Button variant="ghost" size="xs" onClick={initRoots} className="w-full justify-center">
          <RefreshCw size={11} /> Refresh
        </Button>
      </div>
    </div>
  )
}

function TreeNode({ node, depth, expanded, onExpand, selected, onSelect }) {
  const isOpen = expanded[node.path]
  const isSelected = selected === node.path
  const Icon = isOpen ? FolderOpen : (node.type === 'drive' ? HardDrive : Folder)
  const iconColor = node.type === 'drive'
    ? 'text-cyber-muted'
    : isOpen ? 'text-amber-300' : 'text-amber-400'
  const Chevron = isOpen ? ChevronDown : ChevronRight

  return (
    <div>
      <div
        onClick={() => { onExpand(node); onSelect(node.path) }}
        style={{ paddingLeft: `${depth * 12 + 4}px` }}
        className={`flex items-center gap-1.5 py-0.5 pr-2 rounded cursor-pointer select-none
          text-xs transition-colors
          ${isSelected
            ? 'bg-cyber-cyan/10 text-cyber-cyan'
            : 'text-cyber-muted hover:text-cyber-text hover:bg-cyber-tertiary'}`}
      >
        <Chevron size={10} className="flex-shrink-0 opacity-60" />
        <Icon size={12} className={`flex-shrink-0 ${iconColor}`} />
        <span className="truncate">{node.name}</span>
      </div>
      {isOpen && node.children && node.children.map(child => (
        <TreeNode key={child.path} node={child} depth={depth + 1}
          expanded={expanded} onExpand={onExpand}
          selected={selected} onSelect={onSelect} />
      ))}
    </div>
  )
}

// Immutable tree update helper
function updateNode(nodes, targetPath, updater) {
  return nodes.map(n => {
    if (n.path === targetPath) return updater(n)
    if (n.children) return { ...n, children: updateNode(n.children, targetPath, updater) }
    return n
  })
}
