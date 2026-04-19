import { useState, useEffect } from 'react'
import { Save, RotateCcw, Trash2 } from 'lucide-react'
import { Button } from './ui/Button'
import { metalens } from '../api/client'

export function MetadataEditor({ record, filePath, onSaved, onUndoPush }) {
  const [edits, setEdits] = useState({})   // key → new value string
  const [deletes, setDeletes] = useState(new Set())
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)
  const [success, setSuccess] = useState(false)

  useEffect(() => { setEdits({}); setDeletes(new Set()); setError(null); setSuccess(false) }, [filePath])

  if (!record) {
    return <div className="flex items-center justify-center h-full text-cyber-dim text-xs">Select a file to edit metadata</div>
  }
  if (!record.supports_write) {
    return <div className="flex items-center justify-center h-full text-cyber-muted text-xs">This format is read-only</div>
  }

  const editableFields = record.fields.filter(f => f.editable && !f.key.startsWith('img:') && !f.key.startsWith('audio:') && !f.key.startsWith('video:') && !f.key.startsWith('fs:size') && !f.key.startsWith('fs:ctime'))

  async function save() {
    setSaving(true); setError(null); setSuccess(false)
    try {
      const changedFields = editableFields
        .filter(f => f.key in edits)
        .map(f => ({ ...f, value: edits[f.key] }))

      if (changedFields.length > 0) {
        // Store original for undo
        const originals = changedFields.map(f => ({ ...f, value: f.value }))
        onUndoPush({ filePath, fields: originals })
        await metalens.write(filePath, changedFields)
      }

      if (deletes.size > 0) {
        await metalens.delete(filePath, [...deletes])
      }

      setEdits({}); setDeletes(new Set()); setSuccess(true)
      setTimeout(() => setSuccess(false), 2000)
      onSaved()
    } catch (e) {
      setError(e.message)
    } finally {
      setSaving(false)
    }
  }

  function discard() { setEdits({}); setDeletes(new Set()); setError(null) }

  const hasChanges = Object.keys(edits).length > 0 || deletes.size > 0

  return (
    <div className="flex flex-col h-full">
      {/* Toolbar */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-cyber-border bg-cyber-panel">
        <Button variant="primary" size="xs" onClick={save} disabled={!hasChanges || saving}>
          <Save size={11} /> {saving ? 'Saving…' : 'Save'}
        </Button>
        <Button variant="ghost" size="xs" onClick={discard} disabled={!hasChanges}>
          <RotateCcw size={11} /> Discard
        </Button>
        {hasChanges && <span className="text-[10px] text-cyber-warning font-mono ml-auto">
          {Object.keys(edits).length + deletes.size} unsaved change{Object.keys(edits).length + deletes.size !== 1 ? 's' : ''}
        </span>}
        {success && <span className="text-[10px] text-cyber-success font-mono ml-auto">Saved ✓</span>}
        {error && <span className="text-[10px] text-cyber-danger font-mono ml-auto truncate max-w-xs">{error}</span>}
      </div>

      {/* Fields */}
      <div className="flex-1 overflow-y-auto px-3 py-2 space-y-1">
        {editableFields.length === 0 && (
          <div className="text-cyber-dim text-xs text-center py-8">No editable fields</div>
        )}
        {editableFields.map(f => {
          const isDel = deletes.has(f.key)
          const isChanged = f.key in edits
          return (
            <div key={f.key}
              className={`flex items-center gap-2 p-2 rounded border transition-colors
                ${isDel ? 'border-cyber-danger/40 bg-cyber-danger/5 opacity-50' :
                  isChanged ? 'border-cyber-cyan/40 bg-cyber-cyan/5' :
                  'border-cyber-border bg-cyber-panel hover:border-cyber-border/80'}`}>
              <div className="w-36 flex-shrink-0 text-[11px] text-cyber-muted font-mono truncate"
                title={f.key}>{f.label}</div>
              <input
                type="text"
                value={isDel ? '(deleted)' : (edits[f.key] ?? String(f.value ?? ''))}
                disabled={isDel}
                onChange={e => setEdits(prev => ({ ...prev, [f.key]: e.target.value }))}
                className="flex-1 bg-cyber-bg border border-cyber-border rounded px-2 py-0.5
                  text-xs font-mono text-cyber-text focus:border-cyber-cyan focus:outline-none
                  disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
              />
              {f.deletable && !isDel && (
                <button onClick={() => setDeletes(prev => new Set([...prev, f.key]))}
                  className="text-cyber-dim hover:text-cyber-danger transition-colors flex-shrink-0">
                  <Trash2 size={12} />
                </button>
              )}
              {isDel && (
                <button onClick={() => setDeletes(prev => { const n = new Set(prev); n.delete(f.key); return n })}
                  className="text-cyber-dim hover:text-cyber-warning transition-colors flex-shrink-0 text-[10px] font-mono">
                  undo
                </button>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
