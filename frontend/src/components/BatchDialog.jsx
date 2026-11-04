import { useState } from 'react'
import { X, ShieldCheck, CalendarClock, Copy, Pencil, Download, MapPin } from 'lucide-react'
import { Button } from './ui/Button'
import { Spinner } from './ui/Spinner'
import { metalens } from '../api/client'
import { PRESETS } from './SharePanel'

const ACTIONS = [
  { id: 'share', label: 'Prepare for sharing', Icon: ShieldCheck },
  { id: 'dates', label: 'Shift dates', Icon: CalendarClock },
  { id: 'copy', label: 'Copy tags', Icon: Copy },
  { id: 'place', label: 'Add location', Icon: MapPin },
  { id: 'rename', label: 'Rename', Icon: Pencil },
  { id: 'export', label: 'Export', Icon: Download },
]
const UNITS = { minutes: 60, hours: 3600, days: 86400 }
const baseName = path => String(path).split(/[\\/]/).pop()

/** Save `text` as a file through the browser (no network: the content is already in memory). */
export function download(name, text, type) {
  const url = URL.createObjectURL(new Blob([text], { type }))
  const link = document.createElement('a')
  link.href = url
  link.download = name
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

function Results({ response, describe }) {
  if (!response) return null
  return (
    <ul aria-label="Results" className="max-h-48 overflow-y-auto border border-cyber-border rounded p-2 font-mono text-[11px] space-y-1">
      {response.results.map(r => (
        <li key={r.path} className={r.ok ? '' : 'text-cyber-danger'}>
          <span className="text-cyber-cyan">{baseName(r.path)}</span>{' '}
          {r.ok ? describe(r, response.preview) : `not done: ${(r.errors ?? []).map(e => e.message).join('; ') || 'error'}`}
        </li>
      ))}
    </ul>
  )
}

export function BatchDialog({ paths, folder, onClose, onUndoPush, runExclusive = fn => fn(), onChanged }) {
  const [action, setAction] = useState('share')
  const [preset, setPreset] = useState(PRESETS[1])
  const [amount, setAmount] = useState(1)
  const [unit, setUnit] = useState('hours')
  const [template, setTemplate] = useState('{exif:DateTimeOriginal|%Y%m%d_%H%M%S}_{name}')
  const [format, setFormat] = useState('csv')
  const [latitude, setLatitude] = useState('')
  const [longitude, setLongitude] = useState('')
  const [gpx, setGpx] = useState('')
  const [offset, setOffset] = useState(0)
  const [response, setResponse] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const seconds = Math.round(Number(amount) * UNITS[unit])

  const run = async (preview) => {
    setBusy(true)
    setError(null)
    try {
      let body
      if (action === 'share') body = await metalens.sanitizeMany(paths, preset.categories, { preview })
      else if (action === 'dates') body = await metalens.shiftDates(paths, seconds, { preview })
      else if (action === 'place') {
        body = await metalens.geotag(paths, gpx.trim()
          ? { gpx: gpx.trim(), utc_offset_minutes: Math.round(Number(offset) * 60) }
          : { latitude: Number(latitude), longitude: Number(longitude) }, { preview })
      } else if (action === 'copy') body = await metalens.copyTags(paths[0], paths.slice(1), { preview })
      else if (action === 'rename') {
        const results = []
        for (const path of paths) {
          try { results.push({ path, ok: true, ...(await metalens.rename(path, template, { preview })) }) }
          catch (e) { results.push({ path, ok: false, detail: e.message }) }
        }
        body = { ok: results.every(r => r.ok), results, undo_tokens: [] }
      }
      setResponse({ ...body, preview })
      if (!preview) {
        for (const r of body.results ?? []) {
          if (r.undo_token) onUndoPush?.({ filePath: r.path, token: r.undo_token, label: ACTIONS.find(a => a.id === action).label })
        }
        onChanged?.()
      }
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }
  const runExclusiveApply = () => runExclusive(() => run(false))

  const exportFolder = async () => {
    setBusy(true)
    setError(null)
    try {
      const text = await metalens.exportFolder(folder, format)
      download(`metadata.${format}`, text, format === 'csv' ? 'text/csv' : 'application/json')
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const needsTwo = (action === 'copy' && paths.length < 2)
    || (action === 'place' && !gpx.trim() && (latitude === '' || longitude === ''))
  const describe = {
    share: (r, preview) => preview ? `${r.will_remove?.length ?? 0} to remove${r.remaining?.length ? `, ${r.remaining.length} cannot be removed` : ''}`
      : `removed ${r.removed?.length ?? 0}`,
    dates: r => r.changes?.length ? `${r.changes.length} date${r.changes.length > 1 ? 's' : ''}` : 'no dates to change',
    place: r => r.will_write?.length ? `position ${r.position?.slice(0, 2).join(', ')}` : (r.reason ?? 'no position'),
    copy: r => r.will_copy?.length ? `${r.will_copy.length} field${r.will_copy.length > 1 ? 's' : ''}` : 'nothing to copy',
    rename: r => (r.renamed ? 'renamed to ' : 'would become ') + baseName(r.new ?? ''),
  }[action]

  return (
    <div role="dialog" aria-label="Batch" aria-modal="true"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <div className="w-[34rem] max-w-[95vw] max-h-[90vh] overflow-y-auto bg-cyber-panel border border-cyber-border rounded p-4 space-y-3 text-xs">
        <div className="flex items-center">
          <span className="font-mono text-cyber-cyan">
            {action === 'export' ? 'Export the metadata of the folder' : `${paths.length} file${paths.length !== 1 ? 's' : ''} selected`}
          </span>
          <button aria-label="Close" className="ml-auto text-cyber-muted hover:text-cyber-text" onClick={onClose}><X size={14} /></button>
        </div>

        <div className="flex flex-wrap gap-1" role="tablist" aria-label="Action">
          {ACTIONS.map(({ id, label, Icon }) => (
            <button key={id} role="tab" aria-selected={action === id} onClick={() => { setAction(id); setResponse(null); setError(null) }}
              className={`flex items-center gap-1 px-2 py-1 rounded border text-[11px]
                ${action === id ? 'border-cyber-cyan text-cyber-cyan bg-cyber-cyan/5' : 'border-cyber-border text-cyber-muted'}`}>
              <Icon size={11} aria-hidden="true" /> {label}
            </button>
          ))}
        </div>

        {action === 'share' && (
          <div className="flex flex-wrap gap-1.5" role="group" aria-label="Presets">
            {PRESETS.map(p => (
              <Button key={p.id} size="xs" variant={preset.id === p.id ? 'primary' : 'ghost'} onClick={() => { setPreset(p); setResponse(null) }}>
                {p.label}
              </Button>
            ))}
          </div>
        )}
        {action === 'dates' && (
          <div className="flex items-center gap-2">
            <label>Move by <input aria-label="Amount" type="number" value={amount} onChange={e => { setAmount(e.target.value); setResponse(null) }}
              className="w-20 bg-cyber-bg border border-cyber-border rounded px-1" /></label>
            <select aria-label="Unit" value={unit} onChange={e => { setUnit(e.target.value); setResponse(null) }}
              className="bg-cyber-bg border border-cyber-border rounded px-1">
              {Object.keys(UNITS).map(u => <option key={u}>{u}</option>)}
            </select>
            <span className="text-cyber-dim">negative values move the dates back</span>
          </div>
        )}
        {action === 'copy' && (
          <p className="text-cyber-muted">The metadata of <span className="text-cyber-cyan">{baseName(paths[0] ?? '')}</span> (the first
            selected file) is copied to the other {Math.max(paths.length - 1, 0)} selected file(s) where the field exists.</p>
        )}
        {action === 'place' && (
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <label>Latitude <input aria-label="Latitude" type="number" step="any" value={latitude} onChange={e => { setLatitude(e.target.value); setResponse(null) }}
                className="w-28 bg-cyber-bg border border-cyber-border rounded px-1" /></label>
              <label>Longitude <input aria-label="Longitude" type="number" step="any" value={longitude} onChange={e => { setLongitude(e.target.value); setResponse(null) }}
                className="w-28 bg-cyber-bg border border-cyber-border rounded px-1" /></label>
            </div>
            <label className="block">or a GPX track (path of the file)
              <input aria-label="GPX file" value={gpx} onChange={e => { setGpx(e.target.value); setResponse(null) }}
                className="mt-1 w-full bg-cyber-bg border border-cyber-border rounded px-2 py-1 font-mono" /></label>
            {gpx.trim() && (
              <label>Camera clock minus UTC (hours) <input aria-label="Clock offset" type="number" step="0.5" value={offset}
                onChange={e => { setOffset(e.target.value); setResponse(null) }} className="w-20 bg-cyber-bg border border-cyber-border rounded px-1" /></label>
            )}
          </div>
        )}
        {action === 'rename' && (
          <label className="block">Name template
            <input aria-label="Template" value={template} onChange={e => { setTemplate(e.target.value); setResponse(null) }}
              className="mt-1 w-full bg-cyber-bg border border-cyber-border rounded px-2 py-1 font-mono" />
            <span className="text-cyber-dim">{'{name}'}, {'{ext}'}, {'{exif:Make}'}, {'{exif:DateTimeOriginal|%Y%m%d}'}</span>
          </label>
        )}
        {action === 'export' && (
          <div className="flex items-center gap-2">
            <select aria-label="Format" value={format} onChange={e => setFormat(e.target.value)}
              className="bg-cyber-bg border border-cyber-border rounded px-1">
              <option value="csv">CSV</option><option value="json">JSON</option>
            </select>
            <Button variant="primary" size="xs" onClick={exportFolder} disabled={busy || !folder}>
              {busy ? <Spinner size={11} /> : <Download size={11} />} Save the metadata of every file in the folder
            </Button>
          </div>
        )}

        {error && <div role="alert" className="text-cyber-danger font-mono">{error}</div>}

        {action !== 'export' && (
          <>
            <Results response={response} describe={describe} />
            <div className="flex gap-2">
              <Button size="xs" variant="ghost" onClick={() => run(true)} disabled={busy || needsTwo}>Preview</Button>
              <Button size="xs" variant="primary" onClick={runExclusiveApply}
                disabled={busy || needsTwo || !response || !response.preview}>
                {busy ? <Spinner size={11} /> : null} Apply to {action === 'copy' ? Math.max(paths.length - 1, 0) : paths.length} file(s)
              </Button>
            </div>
            {response && !response.preview && <div role="status" className="text-cyber-success">Done.</div>}
          </>
        )}
      </div>
    </div>
  )
}
