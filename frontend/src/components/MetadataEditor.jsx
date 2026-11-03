import { useState, useEffect, useRef } from 'react'
import { Save, RotateCcw, Trash2, Plus, X } from 'lucide-react'
import { Button } from './ui/Button'
import { FieldInput } from './FieldInput'
import { FieldValue } from './MetadataTable'
import { PrivacySummary, SensitivityBadge } from './PrivacyIndicators'
import { RecordNotices } from './RecordNotices'
import { metalens } from '../api/client'
import { emptyInput, fromInput, inputKind, toInput } from '../lib/serializeValue'
import { uniqueKeys } from '../lib/formatValue'

const immediately = operation => operation()

// Human-readable summary of one save, shown when it is undone ("Edit Title; Delete Cover").
const fieldNames = fields => fields.map(f => f.label || f.key).join(', ')
function describeSave(edited, added, deleted) {
  return [['Edit', edited], ['Add', added], ['Delete', deleted]]
    .filter(([, fields]) => fields.length)
    .map(([verb, fields]) => `${verb} ${fieldNames(fields)}`)
    .join('; ')
}

// Sections by key namespace. Grouping is only presentation: editability comes
// exclusively from the server's `editable` / `deletable` flags.
const GROUPS = [
  ['exif', 'EXIF'], ['gps', 'GPS'], ['img', 'Image'], ['xmp', 'XMP'], ['iptc', 'IPTC'],
  ['png', 'PNG'], ['pngchunk', 'PNG chunks'], ['heif', 'HEIF'], ['jfif', 'JFIF'], ['jpeg', 'JPEG'], ['mpf', 'MPF'], ['icc', 'ICC profile'], ['gif', 'GIF'], ['tag', 'Tags'], ['vorbis', 'Vorbis'], ['flac', 'FLAC'],
  ['mp4', 'MP4'], ['qt', 'QuickTime'], ['mkv', 'Matroska'], ['asf', 'ASF'], ['ape', 'APE'], ['audio', 'Audio'], ['video', 'Video'],
  ['pdf', 'PDF'], ['office', 'Office'], ['odf', 'OpenDocument'], ['epub', 'EPUB'], ['ole', 'OLE'], ['hachoir', 'Hachoir'],
  ['other', 'Other'], ['fs', 'Filesystem'], ['xattr', 'Extended attributes'],
]
const GROUP_LABELS = Object.fromEntries(GROUPS)
const GROUP_ORDER = Object.fromEntries(GROUPS.map(([id], i) => [id, i]))
const groupOf = key => {
  const prefix = String(key).split(':', 1)[0]
  return String(key).includes(':') && prefix in GROUP_LABELS && prefix !== 'other' ? prefix : 'other'
}

export function MetadataEditor({ record, filePath, onSaved, onUndoPush, runExclusive = immediately, mutationBusy = false }) {
  const [edits, setEdits] = useState({})   // key → input draft (see lib/serializeValue)
  const [deletes, setDeletes] = useState(new Set())
  const [additions, setAdditions] = useState({})   // key → draft of a field from addable_fields
  const [addKey, setAddKey] = useState('')
  const [addDraft, setAddDraft] = useState(null)
  const [fieldErrors, setFieldErrors] = useState({})
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)
  const [success, setSuccess] = useState(false)
  const [undoNote, setUndoNote] = useState(null)   // set when a change was saved but cannot be undone
  const saveInFlight = useRef(false)
  const generation = useRef(0)

  useEffect(() => {
    setEdits({}); setDeletes(new Set()); setAdditions({}); setAddKey(''); setAddDraft(null)
    setFieldErrors({}); setError(null); setSuccess(false); setUndoNote(null)
    generation.current += 1
    return () => { generation.current += 1 }
  }, [filePath])

  if (!record) {
    return <div className="flex items-center justify-center h-full text-cyber-dim text-xs">Select a file to edit metadata</div>
  }

  const rows = record.fields.filter(f => f.editable || f.deletable)
  const addable = (record.addable_fields ?? []).filter(f => f.editable !== false && inputKind(f) !== 'binary')
  const addableByKey = Object.fromEntries(addable.map(f => [f.key, f]))
  const present = new Set(record.fields.map(f => f.key))
  const offered = addable.filter(f => !present.has(f.key) && !(f.key in additions))
  const added = Object.keys(additions).filter(key => addableByKey[key]).map(key => addableByKey[key])
  const busy = saving || mutationBusy

  function clearFieldError(key) {
    setFieldErrors(prev => {
      if (!(key in prev)) return prev
      const next = { ...prev }; delete next[key]; return next
    })
  }

  async function save() {
    if (saveInFlight.current || mutationBusy) return
    // Validate every pending value before touching the file.
    const invalid = {}
    const changedOriginals = rows.filter(f => f.editable && f.key in edits && !deletes.has(f.key))
    const writes = []
    for (const f of changedOriginals) {
      try { writes.push({ key: f.key, value: fromInput(f, edits[f.key]) }) } catch (e) { invalid[f.key] = e.message }
    }
    for (const f of added) {
      try { writes.push({ key: f.key, value: fromInput(f, additions[f.key]) }) } catch (e) { invalid[f.key] = e.message }
    }
    const invalidCount = Object.keys(invalid).length
    if (invalidCount) {
      setFieldErrors(invalid); setSuccess(false)
      setError(`${invalidCount} invalid value${invalidCount !== 1 ? 's' : ''}: fix ${invalidCount !== 1 ? 'them' : 'it'} before saving`)
      return
    }
    const deleteKeys = [...deletes]
    const label = describeSave(changedOriginals, added, rows.filter(f => deletes.has(f.key)))

    saveInFlight.current = true
    const version = generation.current
    const current = () => version === generation.current
    let unavailable = null
    let saveWarnings = []
    setSaving(true); setError(null); setFieldErrors({}); setSuccess(false); setUndoNote(null)
    try {
      await runExclusive(async () => {
        // One atomic save (D2): edits, additions and deletions succeed or fail together,
        // so there is exactly one undo token and never a partially applied save.
        const result = await metalens.write(filePath, writes, deleteKeys)
        // A null token means nothing actually changed: no undo entry. The entry keeps
        // the original file even if the user switched files meanwhile.
        if (result?.undo_token) onUndoPush?.({ filePath, token: result.undo_token, label })
        // The server reports (instead of silently dropping) a saved change it cannot undo.
        else unavailable = result?.undo_unavailable ?? null
        saveWarnings = Array.isArray(result?.warnings) ? result.warnings.filter(Boolean) : []
      })
      if (current()) {
        setEdits({}); setDeletes(new Set()); setAdditions({}); setSuccess(true)
        setUndoNote([unavailable?.message, ...saveWarnings].filter(Boolean).join(' · ') || null)
        onSaved()
      }
    } catch (e) {
      if (current()) {
        setError(e.message)
        const byKey = {}
        for (const entry of e.errors ?? []) {
          if (entry?.key) byKey[entry.key] = byKey[entry.key] ? `${byKey[entry.key]}; ${entry.message}` : entry.message
        }
        setFieldErrors(byKey)
      }
    } finally {
      saveInFlight.current = false
      if (current()) setSaving(false)
    }
  }

  function discard() {
    setEdits({}); setDeletes(new Set()); setAdditions({}); setAddKey(''); setAddDraft(null)
    setFieldErrors({}); setError(null)
  }

  function chooseAddable(key) {
    setAddKey(key)
    setAddDraft(key && addableByKey[key] ? emptyInput(addableByKey[key]) : null)
  }

  function addField() {
    if (!addKey || !addableByKey[addKey]) return
    setAdditions(prev => ({ ...prev, [addKey]: addDraft }))
    setAddKey(''); setAddDraft(null); setSuccess(false)
  }

  function removeAddition(key) {
    setAdditions(prev => { const next = { ...prev }; delete next[key]; return next })
    clearFieldError(key)
  }

  const changeCount = Object.keys(edits).length + deletes.size + added.length
  const hasChanges = changeCount > 0

  // Rows grouped by namespace; pending additions appear in their group.
  const items = [...rows.map(f => ({ field: f, isNew: false })), ...added.map(f => ({ field: f, isNew: true }))]
  const groups = new Map()
  for (const item of items) {
    const g = groupOf(item.field.key)
    if (!groups.has(g)) groups.set(g, [])
    groups.get(g).push(item)
  }
  const orderedGroups = [...groups.entries()].sort(([a], [b]) => GROUP_ORDER[a] - GROUP_ORDER[b])
  const readOnlyFile = record.supports_write === false && record.supports_delete === false

  return (
    <div className="flex flex-col h-full">
      {/* Toolbar */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-cyber-border bg-cyber-panel">
        <Button variant="primary" size="xs" onClick={save} disabled={!hasChanges || busy}>
          <Save size={11} /> {saving ? 'Saving…' : 'Save'}
        </Button>
        <Button variant="ghost" size="xs" onClick={discard} disabled={!hasChanges || busy}>
          <RotateCcw size={11} /> Discard
        </Button>
        {hasChanges && <span className="text-[10px] text-cyber-warning font-mono ml-auto">
          {changeCount} unsaved change{changeCount !== 1 ? 's' : ''}
        </span>}
        {success && <span className="text-[10px] text-cyber-success font-mono ml-auto">Saved ✓</span>}
        {undoNote && !hasChanges && (
          <span role="status" className="text-[10px] text-cyber-warning font-mono truncate max-w-xs" title={undoNote}>
            {undoNote}
          </span>
        )}
        {error && <span className="text-[10px] text-cyber-danger font-mono ml-auto truncate max-w-xs" title={error}>{error}</span>}
      </div>

      <RecordNotices record={record} showReadOnly />
      <PrivacySummary fields={record?.fields ?? []} />

      {/* Fields */}
      <div className="flex-1 overflow-y-auto px-3 py-2 space-y-2">
        {items.length === 0 && (
          <div className="text-cyber-dim text-xs text-center py-8">
            {readOnlyFile && !offered.length ? 'This file is read-only' : 'No editable fields'}
          </div>
        )}
        {orderedGroups.map(([group, groupItems]) => {
          const keys = uniqueKeys(groupItems, item => item.field.key)
          return (
            <section key={group} className="space-y-1">
              <div className="flex items-center gap-2 text-[10px] font-mono text-cyber-muted uppercase tracking-wider">
                <span>{GROUP_LABELS[group]}</span>
                <span className="text-cyber-dim">{groupItems.length}</span>
              </div>
              {groupItems.map(({ field: f, isNew }, i) => (
                <FieldRow key={keys[i]} field={f} isNew={isNew}
                  draft={isNew ? additions[f.key] : (f.key in edits ? edits[f.key] : toInput(f))}
                  isChanged={isNew || f.key in edits} isDeleted={!isNew && deletes.has(f.key)}
                  error={fieldErrors[f.key]} disabled={busy}
                  onChange={draft => {
                    clearFieldError(f.key); setSuccess(false)
                    if (isNew) setAdditions(prev => ({ ...prev, [f.key]: draft }))
                    else setEdits(prev => ({ ...prev, [f.key]: draft }))
                  }}
                  onDelete={() => setDeletes(prev => new Set([...prev, f.key]))}
                  onRestore={() => setDeletes(prev => { const n = new Set(prev); n.delete(f.key); return n })}
                  onRemoveNew={() => removeAddition(f.key)}
                />
              ))}
            </section>
          )
        })}

        {(offered.length > 0 || addKey) && (
          <AddFieldSection offered={offered} addKey={addKey} draft={addDraft} field={addableByKey[addKey]}
            disabled={busy} onChoose={chooseAddable} onChange={setAddDraft} onAdd={addField} />
        )}
      </div>
    </div>
  )
}

function FieldRow({ field: f, draft, isNew, isChanged, isDeleted, error, disabled,
  onChange, onDelete, onRestore, onRemoveNew }) {
  const binary = inputKind(f) === 'binary'
  return (
    <div data-field-key={f.key}
      className={`flex items-start gap-2 p-2 rounded border transition-colors
        ${isDeleted ? 'border-cyber-danger/40 bg-cyber-danger/5 opacity-50' :
          error ? 'border-cyber-danger/60 bg-cyber-danger/5' :
          isChanged ? 'border-cyber-cyan/40 bg-cyber-cyan/5' :
          'border-cyber-border bg-cyber-panel hover:border-cyber-border/80'}`}>
      <div className="w-36 flex-shrink-0 text-[11px] text-cyber-muted font-mono truncate pt-0.5" title={f.key}>
        {f.label || f.key}
        {isNew && <span className="ml-1 text-[9px] text-cyber-cyan">new</span>}
        <SensitivityBadge field={f} />
      </div>
      <div className="flex-1 min-w-0">
        {isDeleted
          ? <span className="text-xs font-mono text-cyber-dim">(deleted)</span>
          : (f.editable && !binary)
            ? <FieldInput field={f} draft={draft} onChange={onChange} label={f.label || f.key}
                disabled={disabled} invalid={Boolean(error)} />
            : <div className="font-mono text-[11px] text-cyber-text break-all pt-0.5"><FieldValue field={f} /></div>}
        {error && <div role="alert" className="mt-0.5 text-[10px] font-mono text-cyber-danger break-all">{error}</div>}
      </div>
      {isNew && (
        <button aria-label={`Remove new ${f.label || f.key}`} disabled={disabled} onClick={onRemoveNew}
          className="text-cyber-dim hover:text-cyber-danger transition-colors flex-shrink-0 pt-0.5">
          <X size={12} />
        </button>
      )}
      {!isNew && f.deletable && !isDeleted && (
        <button aria-label={`Delete ${f.label}`} disabled={disabled} onClick={onDelete}
          className="text-cyber-dim hover:text-cyber-danger transition-colors flex-shrink-0 pt-0.5">
          <Trash2 size={12} />
        </button>
      )}
      {isDeleted && (
        <button disabled={disabled} onClick={onRestore}
          className="text-cyber-dim hover:text-cyber-warning transition-colors flex-shrink-0 text-[10px] font-mono">
          undo
        </button>
      )}
    </div>
  )
}

function AddFieldSection({ offered, addKey, draft, field, disabled, onChoose, onChange, onAdd }) {
  return (
    <section className="mt-3 pt-2 border-t border-cyber-border space-y-1">
      <div className="text-[10px] font-mono text-cyber-muted uppercase tracking-wider">Add field</div>
      <div className="flex items-start gap-2 p-2 rounded border border-dashed border-cyber-border bg-cyber-panel">
        <select aria-label="Field to add" value={addKey} disabled={disabled}
          onChange={e => onChoose(e.target.value)}
          className="w-36 flex-shrink-0 bg-cyber-bg border border-cyber-border rounded px-1 py-0.5 text-[11px]
            font-mono text-cyber-text focus:border-cyber-cyan focus:outline-none disabled:opacity-40">
          <option value="">Choose a field…</option>
          {offered.map(f => <option key={f.key} value={f.key}>{f.label && f.label !== f.key ? `${f.label} (${f.key})` : f.key}</option>)}
        </select>
        <div className="flex-1 min-w-0">
          {field && <FieldInput field={field} draft={draft} onChange={onChange} label={`New ${field.label || field.key}`} disabled={disabled} />}
        </div>
        <Button variant="default" size="xs" onClick={onAdd} disabled={!field || disabled}>
          <Plus size={11} /> Add field
        </Button>
      </div>
    </section>
  )
}
