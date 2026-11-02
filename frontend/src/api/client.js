let _connection = null

async function getConnection() {
  if (_connection) return _connection
  if (!window.electronAPI?.getSidecarConnection) {
    throw new Error('Open MetaLens through Electron to connect to the authenticated sidecar')
  }
  const connection = await window.electronAPI.getSidecarConnection()
  if (!Number.isInteger(connection?.port) || connection.port < 1 || connection.port > 65535 || !connection.token) {
    throw new Error('Invalid sidecar connection')
  }
  _connection = connection
  return connection
}

const text = value => {
  if (typeof value === 'string') return value
  if (value === null || value === undefined) return ''
  try { return JSON.stringify(value) } catch { return String(value) }
}

/** One `{code, key, message}` entry with string message, from the uniform body or a FastAPI detail item. */
function normalizeEntry(entry) {
  if (entry === null || typeof entry !== 'object') return { code: null, key: null, message: text(entry) }
  if ('message' in entry) {
    return { code: entry.code ?? null, key: entry.key ?? null, message: text(entry.message) }
  }
  // FastAPI/pydantic default: {loc: [...], msg, type}
  const key = Array.isArray(entry.loc) && entry.loc.length ? entry.loc.join('.') : null
  const msg = text(entry.msg ?? entry)
  return { code: entry.type ?? null, key, message: key ? `${key}: ${msg}` : msg }
}

/**
 * Error built from an error response (plan B4): `{ok:false, errors:[{code,key,message}], detail}`.
 * The Error carries `status`, `code` (first error code), `errors` and `detail`; its message is
 * always readable text (never "[object Object]").
 */
export function apiError(status, statusText, body) {
  let errors = []
  if (Array.isArray(body?.errors)) errors = body.errors.map(normalizeEntry)
  else if (Array.isArray(body?.detail)) errors = body.detail.map(normalizeEntry)
  else if (body?.detail && typeof body.detail === 'object') errors = [normalizeEntry(body.detail)]
  const detail = typeof body?.detail === 'string' && body.detail ? body.detail : null
  const message = detail
    || errors.map(e => e.message).filter(Boolean).join('; ')
    || statusText
    || `HTTP ${status}`
  const error = new Error(message)
  error.status = status
  error.code = errors.find(e => e.code)?.code ?? null
  error.errors = errors
  error.detail = detail ?? message
  return error
}

/**
 * JSON.parse that keeps integers beyond 2^53 exact: a JS number would round them (e.g. an MP4
 * `plID` or an ASF QWORD), so they come back as their decimal text. Needs the reviver `source`
 * context (Chromium 114+, so Electron); elsewhere it degrades to plain JSON.parse.
 */
export function parseJson(raw) {
  return JSON.parse(raw, (key, value, context) => (
    typeof value === 'number' && Number.isInteger(value) && !Number.isSafeInteger(value)
      && typeof context?.source === 'string' && /^-?\d+$/.test(context.source)
      ? context.source
      : value
  ))
}

async function readJson(res) {
  return parseJson(await res.text())
}

async function api(method, path, body = null) {
  const { port, token } = await getConnection()
  const url = `http://127.0.0.1:${port}${path}`
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
  }
  if (body !== null) opts.body = JSON.stringify(body)
  const res = await fetch(url, opts)
  if (!res.ok) {
    const err = await readJson(res).catch(() => null)
    throw apiError(res.status, res.statusText, err)
  }
  return readJson(res)
}

// The server reads only key and value: permission/type flags are never sent.
const writeFields = fields => (fields ?? []).map(({ key, value }) => ({ key, value }))

export const metalens = {
  health:    ()                    => api('GET',  '/health'),
  list:      (path)                => api('GET',  `/list?path=${encodeURIComponent(path)}`),
  read:      (path)                => api('GET',  `/read?path=${encodeURIComponent(path)}`),
  // `deleteKeys` (optional) removes fields in the same atomic save. write/delete resolve to
  // `{ok, errors, undo_token}`: `undo_token` is null when nothing actually changed.
  write:     (path, fields, deleteKeys) => api('POST', '/write', deleteKeys?.length
    ? { path, fields: writeFields(fields), delete: [...deleteKeys] }
    : { path, fields: writeFields(fields) }),
  delete:    (path, keys)          => api('POST', '/delete', { path, keys }),
  // Exact undo of one save by its token (no path: the server journal knows the file).
  // Rejects with status 410 `undo_expired` or 409 `concurrent_modification` when the
  // entry can no longer be applied; other errors (403/422/500/network) are retryable.
  undo:      (token)               => api('POST', '/undo', { token }),
  diff:      (a, b)                => api('GET',  `/diff?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`),
  hash:      (path, algorithms)    => api('GET',  `/hash?path=${encodeURIComponent(path)}&algorithms=${encodeURIComponent(algorithms)}`),
}
