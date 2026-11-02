// Editor drafts <-> canonical JSON values of the write contract (python/core/values.py).
//
// `toInput(field)` turns the server value into the state of the typed input,
// `fromInput(field, draft)` validates that state and returns the value to send:
// lists as arrays, int/float/duration as numbers, bool as boolean, datetime as
// ISO 8601 WITH offset, localdatetime WITHOUT offset, rational as "n/d" text.
// It throws `InputError` with a readable message; the server stays the final
// authority (it re-validates and canonicalizes everything).

export class InputError extends Error {}

const fail = message => { throw new InputError(message) }

export const isList = field => Boolean(field?.multi) || field?.length != null || Array.isArray(field?.value)
export const itemField = field => ({ ...field, multi: false, length: null })
const hasChoices = field => Array.isArray(field?.choices) && field.choices.length > 0

/** Kind of input for one item of the field. */
export function inputKind(field) {
  const type = field?.field_type || 'str'
  if (type === 'binary') return 'binary'
  if (type === 'enum' || hasChoices(field)) return 'select'
  if (['bool', 'int', 'float', 'duration', 'datetime', 'localdatetime', 'rational', 'text'].includes(type)) return type
  return 'str'
}

// ── date-times ────────────────────────────────────────────────────────────────

const ISO_RE = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(?::(\d{2})(\.\d+)?)?\s*(Z|[+-]\d{2}(?::?\d{2})?)?$/i
const LOCAL_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,3})?)?$/
const pad = n => String(Math.abs(n)).padStart(2, '0')

export function normalizeOffset(text) {
  const t = String(text ?? '').trim()
  if (/^z$/i.test(t)) return '+00:00'
  const m = /^([+-])(\d{2}):?(\d{2})?$/.exec(t)
  if (!m || Number(m[2]) > 23 || Number(m[3] ?? 0) > 59) return null
  return `${m[1]}${m[2]}:${m[3] ?? '00'}`
}

function localOffset(date) {
  const minutes = -date.getTimezoneOffset()
  return `${minutes < 0 ? '-' : '+'}${pad(Math.trunc(minutes / 60))}:${pad(minutes % 60)}`
}

function localNow(date = new Date()) {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

function dateDraft(value, withOffset) {
  const m = typeof value === 'string' ? ISO_RE.exec(value.trim()) : null
  if (!m) {
    const now = new Date()
    return { local: '', offset: withOffset ? localOffset(now) : '', frac: '', origLocal: null }
  }
  const local = `${m[1]}T${m[2]}:${m[3] ?? '00'}`
  return { local, offset: m[5] ? normalizeOffset(m[5]) ?? m[5] : '', frac: m[4] ?? '', origLocal: local }
}

function dateValue(draft, withOffset) {
  const local = String(draft?.local ?? '').trim()
  if (!LOCAL_RE.test(local)) fail('Enter a date and time')
  let text = local.length === 16 ? `${local}:00` : local
  // Sub-second digits the input cannot show are kept while the time is unchanged.
  if (draft.frac && local === draft.origLocal) text += draft.frac
  if (!withOffset) return text
  const offset = normalizeOffset(draft.offset)
  if (!offset) fail('Enter a UTC offset such as +02:00 or -05:30')
  return text + offset
}

// ── items ─────────────────────────────────────────────────────────────────────

function itemToInput(field, value) {
  switch (inputKind(field)) {
    case 'binary': return null
    case 'bool': return value === true || value === 'true'
    case 'select': {
      const idx = field.choices.findIndex(c => c === value || String(c) === String(value))
      return String(idx)
    }
    case 'datetime': return dateDraft(value, true)
    case 'localdatetime': return dateDraft(value, false)
    default:
      if (value === null || value === undefined) return ''
      if (typeof value === 'string') return value
      if (typeof value === 'number' || typeof value === 'boolean') return String(value)
      return JSON.stringify(value)
  }
}

function emptyItem(field) {
  switch (inputKind(field)) {
    case 'bool': return false
    case 'select': return '0'
    case 'datetime': { const now = new Date(); return { local: localNow(now), offset: localOffset(now), frac: '', origLocal: null } }
    case 'localdatetime': return { local: localNow(), offset: '', frac: '', origLocal: null }
    case 'binary': return null
    default: return ''
  }
}

function numberText(draft) {
  return String(draft ?? '').trim()
}

function itemFromInput(field, draft) {
  switch (inputKind(field)) {
    case 'binary': return fail('Binary values are read-only')
    case 'str':
    case 'text':
      return typeof draft === 'string' ? draft : fail('Enter a text value')
    case 'bool': return draft === true
    case 'select': {
      const idx = Number(draft)
      if (!Number.isInteger(idx) || idx < 0 || idx >= field.choices.length) fail('Choose a value')
      return field.choices[idx]
    }
    case 'int': {
      const t = numberText(draft)
      if (!/^[+-]?\d+$/.test(t)) fail('Enter an integer')
      const n = Number(t)
      if (Number.isSafeInteger(n)) return n
      // Beyond 2^53 a JS number is inexact: send the exact decimal text instead (the server
      // parses it exactly and applies the field's own range). 64-bit integers are the widest
      // value any format here stores.
      const wide = BigInt(t)
      if (wide < -(2n ** 63n) || wide >= 2n ** 64n) fail('Integer too large')
      return wide.toString()
    }
    case 'float':
    case 'duration': {
      const t = numberText(draft)
      if (!/^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/.test(t)) fail('Enter a number')
      const n = Number(t)
      if (!Number.isFinite(n)) fail('Enter a finite number')
      if (inputKind(field) === 'duration' && n < 0) fail('Duration must be >= 0 seconds')
      return n
    }
    case 'rational': {
      const t = String(draft ?? '')
      const m = /^\s*([+-]?\d+)\s*\/\s*(\d+)\s*$/.exec(t)
      if (m) {
        const num = String(BigInt(m[1])), den = String(BigInt(m[2]))
        if (den === '0' && num !== '0') fail('The denominator n/d cannot be 0')
        return `${num}/${den}`
      }
      // Decimals are converted exactly by the server ("12.34" -> "1234/100").
      if (/^\s*[+-]?(?:\d+\.?\d*|\.\d+)\s*$/.test(t)) return t.trim()
      return fail('Enter a fraction n/d (e.g. 1/125) or a decimal')
    }
    case 'datetime': return dateValue(draft, true)
    case 'localdatetime': return dateValue(draft, false)
    default: return fail('Unsupported value')
  }
}

// ── public API ────────────────────────────────────────────────────────────────

/** Draft for the input from the field's current (canonical) value. */
export function toInput(field, value = field?.value) {
  if (!isList(field)) return itemToInput(field, value)
  const item = itemField(field)
  const items = Array.isArray(value) ? value.map(v => itemToInput(item, v))
    : (value === null || value === undefined ? [] : [itemToInput(item, value)])
  if (field.length != null) {
    while (items.length < field.length) items.push(emptyItem(item))
  }
  return items
}

/** Draft for a field that does not exist yet (Add field). */
export function emptyInput(field) {
  if (!isList(field)) return emptyItem(field)
  const item = itemField(field)
  return Array.from({ length: field.length ?? 1 }, () => emptyItem(item))
}

/** New draft with one more (empty) list item. */
export function addItem(field, draft) {
  return [...(Array.isArray(draft) ? draft : []), emptyItem(itemField(field))]
}

/** Canonical JSON value to send for this draft; throws `InputError` if invalid. */
export function fromInput(field, draft) {
  if (!isList(field)) return itemFromInput(field, draft)
  if (inputKind(field) === 'binary') fail('Binary values are read-only')
  if (!Array.isArray(draft)) fail('A list of values is required')
  if (field.length != null && draft.length !== field.length) fail(`Exactly ${field.length} values are required`)
  if (draft.length === 0) fail('Enter at least one value (or delete the field)')
  const item = itemField(field)
  return draft.map((d, i) => {
    try {
      return itemFromInput(item, d)
    } catch (e) {
      if (e instanceof InputError && draft.length > 1) fail(`Value ${i + 1}: ${e.message}`)
      throw e
    }
  })
}
