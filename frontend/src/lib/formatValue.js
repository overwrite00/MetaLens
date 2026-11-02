// Readable text for canonical metadata values (see python/core/values.py).
// Shared by the metadata table, the diff view and the CSV export so that the
// three always agree. Nothing here may lose information except binary hashes,
// which are abbreviated on screen unless `full` is requested.

export const LIST_SEPARATOR = '; '

export function isBinaryDescriptor(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    && typeof value.size === 'number' && typeof value.sha256 === 'string'
}

function formatBytes(size) {
  if (size < 1024) return null
  const units = ['KiB', 'MiB', 'GiB', 'TiB']
  let n = size / 1024, i = 0
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1 }
  return `${n.toFixed(n < 10 ? 2 : 1)} ${units[i]}`
}

export function formatBinary(descriptor, { full = false } = {}) {
  const human = formatBytes(descriptor.size)
  const parts = [`binary · ${human ? `${human} (${descriptor.size} bytes)` : `${descriptor.size} bytes`}`]
  if (descriptor.mime) parts.push(descriptor.mime)
  const hash = full ? descriptor.sha256 : `${descriptor.sha256.slice(0, 12)}…`
  parts.push(`sha256 ${hash}`)
  return parts.join(' · ')
}

/** Seconds as `45.25 s`, `2:05` or `1:02:05.5`; the fractional digits are kept as given. */
export function formatDuration(seconds) {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return null
  const repr = String(seconds)
  if (seconds < 60) return `${repr} s`
  const fraction = /^\d+\.(\d+)$/.exec(repr)?.[1]
  const whole = Math.floor(seconds)
  const h = Math.floor(whole / 3600), m = Math.floor((whole % 3600) / 60), s = whole % 60
  const pad = n => String(n).padStart(2, '0')
  const tail = `${pad(s)}${fraction ? `.${fraction}` : ''}`
  return h > 0 ? `${h}:${pad(m)}:${tail}` : `${m}:${tail}`
}

/** ISO 8601 date-time with a space instead of `T`; offset and fraction preserved. */
export function formatDateTime(value) {
  return typeof value === 'string' ? value.replace(/^(\d{4}-\d{2}-\d{2})T/, '$1 ') : null
}

function formatItem(value, fieldType, rawValue, full) {
  if (value === null || value === undefined) return ''
  if (isBinaryDescriptor(value)) return formatBinary(value, { full })
  if (fieldType === 'duration') {
    const readable = formatDuration(value)
    if (readable !== null) return readable
    if (rawValue !== null && rawValue !== undefined && rawValue !== '') return formatPlain(rawValue)
    return formatPlain(value)
  }
  if ((fieldType === 'datetime' || fieldType === 'localdatetime') && typeof value === 'string') {
    return formatDateTime(value)
  }
  return formatPlain(value)
}

function formatPlain(value) {
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (Array.isArray(value)) return value.map(formatPlain).join(LIST_SEPARATOR)
  if (isBinaryDescriptor(value)) return formatBinary(value)
  try { return JSON.stringify(value) } catch { return String(value) }
}

/** Items of a field value for list-aware rendering (always an array of strings). */
export function formatItems(field, { full = false } = {}) {
  const v = field?.value
  const type = field?.field_type
  if (Array.isArray(v)) {
    const raws = Array.isArray(field.raw_value) ? field.raw_value : []
    return v.map((item, i) => formatItem(item, type, raws[i], full))
  }
  const one = formatItem(v, type, field?.raw_value, full)
  return one === '' ? [] : [one]
}

/** Single-line text of a field value (lists joined with `; `). */
export function formatValue(field, { full = false } = {}) {
  return formatItems(field, { full }).join(LIST_SEPARATOR)
}

/** Tooltip with the exact underlying value where the display is abbreviated or converted. */
export function valueTitle(field) {
  const v = field?.value
  if (field?.field_type === 'duration' && typeof v === 'number') return `${v} s`
  if (isBinaryDescriptor(v) || (Array.isArray(v) && v.some(isBinaryDescriptor))) return formatValue(field, { full: true })
  if (field?.field_type === 'localdatetime') return `${v} (local time, no UTC offset)`
  return undefined
}

/** React keys unique within a list even when `getKey` repeats (`key`, `key#2`, ...). */
export function uniqueKeys(items, getKey) {
  const seen = new Map()
  return items.map(item => {
    const base = String(getKey(item))
    const n = (seen.get(base) ?? 0) + 1
    seen.set(base, n)
    return n === 1 ? base : `${base}#dup${n}`
  })
}
