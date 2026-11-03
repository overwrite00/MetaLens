import { useEffect, useMemo, useState } from 'react'
import { ShieldCheck, Eye } from 'lucide-react'
import { Button } from './ui/Button'
import { Spinner } from './ui/Spinner'
import { Badge } from './ui/Badge'
import { SENSITIVITY, summarizeSensitivity } from './PrivacyIndicators'
import { metalens } from '../api/client'

// Presets of "Prepare for sharing": which categories are removed.
export const PRESETS = [
  { id: 'location', label: 'Remove location', categories: ['location'] },
  { id: 'personal', label: 'Remove personal data', categories: ['location', 'identity', 'device', 'thumbnail', 'history'] },
  { id: 'everything', label: 'Remove everything shown',
    categories: ['location', 'identity', 'device', 'thumbnail', 'history', 'text', 'datetime', 'software'] },
]

const immediately = fn => fn()

export function SharePanel({ record, filePath, onSaved, onUndoPush, runExclusive = immediately, mutationBusy = false }) {
  const [categories, setCategories] = useState(new Set(PRESETS[1].categories))
  const [preview, setPreview] = useState(null)
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const present = useMemo(() => summarizeSensitivity(record?.fields ?? []), [record])
  const list = useMemo(() => [...categories].sort(), [categories])

  useEffect(() => {
    let current = true
    setPreview(null)
    setError(null)
    if (!list.length) return undefined
    setBusy(true)
    metalens.sanitize(filePath, list, { preview: true })
      .then(body => { if (current) setPreview(body) })
      .catch(e => { if (current) setError(e.message) })
      .finally(() => { if (current) setBusy(false) })
    return () => { current = false }
  }, [filePath, list, record])

  const toggle = category => {
    setResult(null)
    setCategories(prev => {
      const next = new Set(prev)
      if (next.has(category)) next.delete(category)
      else next.add(category)
      return next
    })
  }
  const choose = preset => {
    setCategories(new Set(preset.categories))
    setResult(null)
  }

  const apply = async () => {
    setBusy(true)
    setError(null)
    try {
      await runExclusive(async () => {
        const body = await metalens.sanitize(filePath, list, { preview: false })
        if (body?.undo_token) onUndoPush?.({ filePath, token: body.undo_token, label: 'Prepare for sharing' })
        setResult(body)
      })
      onSaved?.()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const removable = preview?.will_remove?.length ?? 0
  const blocked = preview?.remaining ?? []
  const removed = result?.removed?.length ?? 0

  return (
    <div className="flex flex-col h-full overflow-y-auto p-3 gap-3 text-xs">
      <div className="flex items-center gap-2 text-cyber-cyan font-mono">
        <ShieldCheck size={14} aria-hidden="true" /> Prepare this file for sharing
      </div>
      <p className="text-cyber-muted">
        Choose what to remove before you send the file to someone. You see what will be removed first, and the
        change can be undone.
      </p>

      <div className="flex flex-wrap gap-1.5" role="group" aria-label="Presets">
        {PRESETS.map(p => (
          <Button key={p.id} size="xs" variant="ghost" onClick={() => choose(p)}>{p.label}</Button>
        ))}
      </div>

      <fieldset className="border border-cyber-border rounded p-2">
        <legend className="px-1 text-[10px] text-cyber-muted font-mono">Remove</legend>
        {Object.entries(SENSITIVITY).map(([id, info]) => {
          const count = present.find(s => s.category === id)?.count ?? 0
          return (
            <label key={id} className="flex items-center gap-2 py-0.5">
              <input type="checkbox" checked={categories.has(id)} onChange={() => toggle(id)}
                disabled={busy || mutationBusy} />
              <span>{info.label}</span>
              <span className="text-cyber-dim">{info.hint}</span>
              <Badge variant={count ? info.variant : 'default'} className="ml-auto">{count}</Badge>
            </label>
          )
        })}
      </fieldset>

      {error && <div className="text-cyber-danger font-mono" role="alert">{error}</div>}

      {preview && !result && (
        <div aria-label="Preview" className="border border-cyber-border rounded p-2">
          <div className="flex items-center gap-1.5 text-cyber-muted mb-1">
            <Eye size={12} aria-hidden="true" /> Preview
          </div>
          {removable === 0
            ? <div className="text-cyber-dim">Nothing to remove for the chosen categories.</div>
            : (
              <ul className="font-mono text-[11px] list-disc ml-4">
                {preview.will_remove.map(e => (
                  <li key={e.key}>{e.label || e.key} <span className="text-cyber-dim">({e.key})</span></li>
                ))}
              </ul>
            )}
          {blocked.length > 0 && (
            <div className="mt-2 text-cyber-warning">
              {blocked.length} sensitive field{blocked.length > 1 ? 's' : ''} cannot be removed from this file:
              <ul className="font-mono text-[11px] list-disc ml-4">
                {blocked.map(e => <li key={e.key}>{e.label || e.key}: {e.reason}</li>)}
              </ul>
            </div>
          )}
        </div>
      )}

      {result && (
        <div role="status" className="border border-cyber-success/40 rounded p-2 text-cyber-success">
          Removed {removed} field{removed === 1 ? '' : 's'}.
          {result.remaining?.length > 0 && (
            <span className="text-cyber-warning"> {result.remaining.length} could not be removed.</span>
          )}
          {result.warnings?.map((w, i) => <div key={i} className="text-cyber-warning">{w}</div>)}
        </div>
      )}

      <div className="flex gap-2">
        <Button variant="primary" size="xs" onClick={apply} disabled={busy || mutationBusy || !removable}>
          {busy ? <Spinner size={11} /> : <ShieldCheck size={11} />}
          {' '}Remove{removable ? ` ${removable} field${removable > 1 ? 's' : ''}` : ''}
        </Button>
      </div>
    </div>
  )
}
