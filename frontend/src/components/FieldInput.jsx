import { Plus, X } from 'lucide-react'
import { addItem, inputKind, isList, itemField } from '../lib/serializeValue'

const BOX = `bg-cyber-bg border border-cyber-border rounded px-2 py-0.5 text-xs font-mono text-cyber-text
  focus:border-cyber-cyan focus:outline-none disabled:opacity-40 disabled:cursor-not-allowed transition-colors`
const INVALID = 'border-cyber-danger/70'

/**
 * Typed input for a field draft (see lib/serializeValue). `label` is the accessible
 * name of the input (list items: "<label> [n]"). Binary fields render nothing:
 * they are never editable.
 */
export function FieldInput({ field, draft, onChange, label, disabled = false, invalid = false }) {
  if (inputKind(field) === 'binary') return null
  if (isList(field)) {
    return <ListInput field={field} draft={draft} onChange={onChange} label={label} disabled={disabled} invalid={invalid} />
  }
  return <ItemInput field={field} draft={draft} onChange={onChange} label={label} disabled={disabled} invalid={invalid} />
}

function ListInput({ field, draft, onChange, label, disabled, invalid }) {
  const items = Array.isArray(draft) ? draft : []
  const fixed = field.length != null
  const item = itemField(field)
  const set = (i, value) => onChange(items.map((d, n) => (n === i ? value : d)))
  return (
    <div className={`flex flex-col gap-1 ${fixed ? 'sm:flex-row sm:flex-wrap' : ''}`}>
      {items.map((d, i) => (
        <div key={i} className="flex items-center gap-1 min-w-0">
          <div className="flex-1 min-w-0">
            <ItemInput field={item} draft={d} onChange={v => set(i, v)} label={`${label} [${i + 1}]`}
              disabled={disabled} invalid={invalid} />
          </div>
          {!fixed && (
            <button type="button" aria-label={`Remove ${label} value ${i + 1}`} disabled={disabled}
              onClick={() => onChange(items.filter((_, n) => n !== i))}
              className="text-cyber-dim hover:text-cyber-danger transition-colors flex-shrink-0 disabled:opacity-40">
              <X size={11} />
            </button>
          )}
        </div>
      ))}
      {!fixed && (
        <button type="button" aria-label={`Add ${label} value`} disabled={disabled}
          onClick={() => onChange(addItem(field, items))}
          className="self-start flex items-center gap-1 text-[10px] font-mono text-cyber-muted hover:text-cyber-cyan
            transition-colors disabled:opacity-40">
          <Plus size={10} /> value
        </button>
      )}
    </div>
  )
}

function ItemInput({ field, draft, onChange, label, disabled, invalid }) {
  const cls = `${BOX} ${invalid ? INVALID : ''}`
  switch (inputKind(field)) {
    case 'bool':
      return (
        <input type="checkbox" aria-label={label} checked={draft === true} disabled={disabled}
          onChange={e => onChange(e.target.checked)} className="accent-cyber-cyan" />
      )
    case 'select': {
      const idx = Number(draft)
      const known = Number.isInteger(idx) && idx >= 0 && idx < field.choices.length
      return (
        <select aria-label={label} value={known ? String(idx) : '-1'} disabled={disabled}
          onChange={e => onChange(e.target.value)} className={`${cls} w-full`}>
          {!known && <option value="-1" disabled>{field.value == null ? 'Choose…' : `(not in list: ${String(field.value)})`}</option>}
          {field.choices.map((c, i) => <option key={i} value={String(i)}>{String(c)}</option>)}
        </select>
      )
    }
    case 'int':
    case 'float':
    case 'duration':
      return (
        <div className="flex items-center gap-1">
          <input type="number" aria-label={label} value={draft ?? ''} disabled={disabled}
            step={inputKind(field) === 'int' ? 1 : 'any'} min={inputKind(field) === 'duration' ? 0 : undefined}
            onChange={e => onChange(e.target.value)} className={`${cls} flex-1 min-w-0`} />
          {inputKind(field) === 'duration' && <span className="text-[10px] text-cyber-dim font-mono">s</span>}
        </div>
      )
    case 'text':
      return (
        <textarea aria-label={label} value={draft ?? ''} disabled={disabled} rows={3}
          onChange={e => onChange(e.target.value)} className={`${cls} w-full resize-y`} />
      )
    case 'rational':
      return (
        <input type="text" aria-label={label} value={draft ?? ''} disabled={disabled} placeholder="n/d"
          inputMode="decimal" onChange={e => onChange(e.target.value)} className={`${cls} w-full`} />
      )
    case 'datetime':
    case 'localdatetime': {
      const d = draft ?? { local: '', offset: '' }
      const withOffset = inputKind(field) === 'datetime'
      return (
        <div className="flex items-center gap-1 min-w-0">
          <input type="datetime-local" step={1} aria-label={label} value={d.local ?? ''} disabled={disabled}
            onChange={e => onChange({ ...d, local: e.target.value })} className={`${cls} flex-1 min-w-0`} />
          {withOffset
            ? <input type="text" aria-label={`${label} UTC offset`} value={d.offset ?? ''} disabled={disabled}
                placeholder="+00:00" title="UTC offset (±HH:MM)"
                onChange={e => onChange({ ...d, offset: e.target.value })} className={`${cls} w-16`} />
            : <span className="text-[10px] text-cyber-dim font-mono" title="Local time, stored without UTC offset">local</span>}
        </div>
      )
    }
    default:
      return (
        <input type="text" aria-label={label} value={draft ?? ''} disabled={disabled}
          onChange={e => onChange(e.target.value)} className={`${cls} w-full`} />
      )
  }
}
