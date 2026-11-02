import { useState, useEffect, useRef } from 'react'
import { Folder, FolderOpen, HardDrive, Home, ChevronRight, ChevronDown, RefreshCw } from 'lucide-react'
import { Button } from './ui/Button'
import { metalens } from '../api/client'

const EXPAND_ALL_DEPTH = 3

export function FolderPanel({ onSelectPath, onRefresh }) {
  const [tree, setTree] = useState([])
  const [expanded, setExpanded] = useState({})
  const [selected, setSelected] = useState(null)
  const [bookmarks, setBookmarks] = useState([])
  const [error, setError] = useState(null)
  const [menu, setMenu] = useState(null)
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

  // Re-read the tree keeping expansion and selection: reload the children of every
  // loaded folder (and the drive list), then tell the parent to reload the file list.
  // A folder that vanished disappears with its parent's listing; one that is listed but
  // unreadable keeps its previous state. A vanished selection falls back to the nearest
  // existing ancestor.
  async function refresh() {
    if (!tree.length) { await initRoots(); return }
    const version = ++generation.current
    requests.current.clear()
    setError(null)
    async function reloadChildren(node) {
      let data
      try {
        data = await metalens.list(node.path)
      } catch {
        return node
      }
      const old = new Map((node.children || []).map(c => [c.path, c]))
      const children = await Promise.all((data.items || []).filter(i => i.is_dir).map(async i => {
        const prev = old.get(i.path)
        const fresh = { name: i.name, path: i.path, type: 'dir', children: null, loading: false }
        return prev && prev.children ? reloadChildren(prev) : fresh
      }))
      return { ...node, children, loading: false }
    }
    try {
      let roots = tree
      if (window.electronAPI) {
        const drives = await window.electronAPI.listDrives()
        const known = new Map(tree.map(n => [n.path, n]))
        roots = drives.length
          ? drives.map(d => known.get(d) || { name: d, path: d, type: 'drive', children: null, loading: false })
          : tree
      }
      const next = await Promise.all(roots.map(r => r.children ? reloadChildren(r) : r))
      if (version !== generation.current) return
      const paths = new Set()
      ;(function collect(nodes) { nodes.forEach(n => { paths.add(n.path); if (n.children) collect(n.children) }) })(next)
      setTree(next)
      setExpanded(e => Object.fromEntries(Object.entries(e).filter(([p]) => paths.has(p))))
      if (selected && !paths.has(selected)) {
        const alive = await nearestExisting(selected)
        if (version !== generation.current) return
        if (alive !== selected) {
          setSelected(alive)
          if (alive) onSelectPath(alive)
        }
      }
    } catch (e) {
      if (version === generation.current) setError(e.message)
    }
    if (version === generation.current) onRefresh?.()
  }

  useEffect(() => {
    if (!menu) return
    const close = () => setMenu(null)
    const onKey = e => { if (e.key === 'Escape') close() }
    window.addEventListener('click', close)
    window.addEventListener('blur', close)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('click', close)
      window.removeEventListener('blur', close)
      window.removeEventListener('keydown', onKey)
    }
  }, [menu])

  function openMenu(e, node) {
    e.preventDefault()
    select(node.path)
    setMenu({ x: e.clientX, y: e.clientY, node })
  }

  function collapseNode(node) {
    setExpanded(e => ({ ...e, [node.path]: false }))
  }

  function collapseAll() {
    setExpanded({})
  }

  // Expand every folder of the tree: all roots, loading the missing levels. Folders
  // already loaded are always followed; unloaded ones are loaded only down to
  // EXPAND_ALL_DEPTH, so a drive does not trigger a scan of the whole disk.
  async function expandAll() {
    const version = generation.current
    const open = []
    async function walk(n, depth) {
      let children = n.children
      if (!children) {
        const data = await metalens.list(n.path)
        children = (data.items || []).filter(i => i.is_dir)
          .map(i => ({ name: i.name, path: i.path, type: 'dir', children: null, loading: false }))
      }
      open.push(n.path)
      if (depth < EXPAND_ALL_DEPTH || n.children) {
        children = await Promise.all(children.map(c => walk(c, depth + 1).catch(() => c)))
      }
      return { ...n, children, loading: false }
    }
    setError(null)
    try {
      const full = await Promise.all(tree.map(r => walk(r, 0).catch(() => r)))
      if (version !== generation.current) return
      setTree(full)
      setExpanded(Object.fromEntries(open.map(p => [p, true])))
    } catch (err) {
      if (version === generation.current) setError(err.message)
    }
  }

  function select(path) {
    setSelected(path)
    onSelectPath(path)
  }

  // Open `target` in the tree: load and expand every ancestor from its drive root,
  // so the whole path is visible, then select it.
  async function reveal(target) {
    select(target)
    const version = generation.current
    const norm = p => p.replace(/[\\/]+/g, '/').replace(/\/$/, '').toLowerCase()
    const t = norm(target)
    let node = tree.find(r => t === norm(r.path) || t.startsWith(norm(r.path) + '/'))
    if (!node) return
    const chain = [node.path]
    const loaded = new Map()
    try {
      while (norm(node.path) !== t) {
        let children = node.children
        if (!children) {
          const data = await metalens.list(node.path)
          children = (data.items || []).filter(i => i.is_dir)
            .map(i => ({ name: i.name, path: i.path, type: 'dir', children: null, loading: false }))
          loaded.set(node.path, children)
        }
        const next = children.find(c => t === norm(c.path) || t.startsWith(norm(c.path) + '/'))
        if (!next) break
        node = next
        chain.push(node.path)
      }
    } catch (e) {
      if (version === generation.current) setError(e.message)
      return
    }
    if (version !== generation.current) return
    setTree(prev => {
      let out = prev
      loaded.forEach((children, p) => {
        out = updateNode(out, p, n => (n.children ? n : { ...n, children, loading: false }))
      })
      return out
    })
    setExpanded(e => ({ ...e, ...Object.fromEntries(chain.map(p => [p, true])) }))
  }

  return (
    <div className="flex flex-col h-full bg-cyber-panel border-r border-cyber-border">
      {/* Bookmarks */}
      <div className="px-2 py-2 border-b border-cyber-border">
        <div className="text-[10px] text-cyber-muted font-mono uppercase tracking-wider mb-1 px-1">Bookmarks</div>
        {bookmarks.map(bm => (
          <button key={bm.path}
            onClick={() => reveal(bm.path)}
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
            selected={selected} onSelect={select} onContextMenu={openMenu} />
        ))}
      </div>

      {menu && (
        <div role="menu" style={{ left: menu.x, top: menu.y }}
          className="fixed z-50 min-w-[150px] py-1 rounded border border-cyber-border bg-cyber-panel shadow-lg text-xs">
          {[
            ['Expand', () => expand(menu.node), !expanded[menu.node.path]],
            ['Collapse', () => collapseNode(menu.node), !!expanded[menu.node.path]],
            ['Expand all', expandAll, true],
            ['Collapse all', collapseAll, true],
          ].map(([label, action, enabled]) => (
            <button key={label} role="menuitem" disabled={!enabled}
              onClick={() => { setMenu(null); action() }}
              className="block w-full px-3 py-1 text-left text-cyber-text hover:bg-cyber-tertiary disabled:opacity-40 disabled:hover:bg-transparent">
              {label}
            </button>
          ))}
        </div>
      )}

      {/* Refresh */}
      <div className="p-2 border-t border-cyber-border">
        <Button variant="ghost" size="xs" onClick={refresh} className="w-full justify-center">
          <RefreshCw size={11} /> Refresh
        </Button>
      </div>
    </div>
  )
}

function TreeNode({ node, depth, expanded, onExpand, selected, onSelect, onContextMenu }) {
  const isOpen = expanded[node.path]
  const isSelected = selected === node.path
  const Icon = isOpen ? FolderOpen : (node.type === 'drive' ? HardDrive : Folder)
  const iconColor = node.type === 'drive'
    ? 'text-cyber-muted'
    : isOpen ? 'text-amber-300' : 'text-amber-400'
  const Chevron = isOpen ? ChevronDown : ChevronRight
  const rowRef = useRef(null)

  useEffect(() => {
    if (isSelected) rowRef.current?.scrollIntoView?.({ block: 'nearest' })
  }, [isSelected, node.path])

  return (
    <div>
      <div
        ref={rowRef}
        onContextMenu={e => onContextMenu(e, node)}
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
          selected={selected} onSelect={onSelect} onContextMenu={onContextMenu} />
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

// Walk up from `path` to the closest folder the sidecar can still list.
async function nearestExisting(path) {
  let p = path
  while (p) {
    try {
      await metalens.list(p)
      return p
    } catch {
      const i = Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\'))
      if (i < 0) return null
      let parent = i === 0 ? '/' : p.slice(0, i)
      if (/^[A-Za-z]:$/.test(parent)) parent += '\\'
      if (parent === p) return null
      p = parent
    }
  }
  return null
}
