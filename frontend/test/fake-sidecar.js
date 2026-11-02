// In-memory stand-in for the sidecar's mutation contract (plan F3B/F4C), used by
// the Undo tests instead of ad-hoc mocks. It keeps the server-owned schema of each
// key (label, editable, deletable, ...) so a write never "loses" flags, and it
// implements the token journal: /write and /delete answer `undo_token` (null for a
// no-op), /undo restores the pre-images (410 unknown/used token, 409 when a touched
// key no longer holds its post-value, entry kept on 409).

const ABSENT = Symbol('absent')
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b)

export function apiError(status, code, message, key = null) {
  return Object.assign(new Error(message), {
    status, code, errors: [{ code, key, message }], detail: message,
  })
}

export function createFakeSidecar({ fields = [], addable = [] } = {}) {
  const schema = new Map([...addable, ...fields].map(f => [f.key, { ...f }]))
  const values = new Map(fields.map(f => [f.key, f.value]))
  const journal = new Map()
  let nextToken = 1
  let failNext = null   // error thrown by the next mutation or undo
  const current = key => (values.has(key) ? values.get(key) : ABSENT)

  const server = {
    values, journal,
    /** The next mutation/undo call rejects with `error` (simulates 403/500/422/network). */
    failNextWith(error) { failNext = error },
    /** Change a key behind MetaLens's back (another program edited the file). */
    externalEdit(key, value) { values.set(key, value) },

    async read() {
      return {
        supports_write: true, supports_delete: true, warnings: [], read_errors: [],
        fields: [...values].map(([key, value]) => ({ ...schema.get(key), value, raw_value: value })),
        addable_fields: addable.map(f => ({ ...f })),
      }
    },

    async write(path, writes = [], deleteKeys = []) {
      if (failNext) { const e = failNext; failNext = null; throw e }
      if (!writes.length && !deleteKeys.length) throw apiError(422, 'invalid_value', 'nothing to save')
      for (const { key } of writes) {
        const known = schema.get(key)
        if (!known || known.editable === false) throw apiError(422, 'read_only', `${key}: read-only`, key)
      }
      for (const key of deleteKeys) {
        if (!values.has(key)) throw apiError(422, 'not_found', `${key}: not found`, key)
      }
      const pre = {}, post = {}
      for (const { key, value } of writes) {
        if (same(current(key), value)) continue
        pre[key] = current(key); values.set(key, value); post[key] = value
      }
      for (const key of deleteKeys) {
        pre[key] = current(key); values.delete(key); post[key] = ABSENT
      }
      if (!Object.keys(pre).length) return { ok: true, errors: [], undo_token: null }
      const token = `tok-${nextToken++}`
      journal.set(token, { path, pre, post })
      return { ok: true, errors: [], undo_token: token }
    },

    async delete(path, keys) { return server.write(path, [], keys) },

    async undo(token) {
      if (failNext) { const e = failNext; failNext = null; throw e }
      const entry = journal.get(token)
      if (!entry) throw apiError(410, 'undo_expired', 'Undo is no longer available (unknown, already used or expired token)')
      for (const key of Object.keys(entry.post)) {
        if (current(key) !== entry.post[key] && !same(current(key), entry.post[key])) {
          throw apiError(409, 'concurrent_modification', `${key}: modified since the operation; Undo refused`, key)
        }
      }
      for (const [key, value] of Object.entries(entry.pre)) {
        if (value === ABSENT) values.delete(key); else values.set(key, value)
      }
      journal.delete(token)
      return { ok: true, errors: [], keys: Object.keys(entry.pre), undo_token: null }
    },
  }
  return server
}

/** Route the mocked `metalens` client to a fake sidecar. */
export function connectFakeSidecar(metalens, server) {
  metalens.read.mockImplementation(path => server.read(path))
  metalens.write.mockImplementation((path, fields, deleteKeys) => server.write(path, fields, deleteKeys))
  metalens.delete.mockImplementation((path, keys) => server.delete(path, keys))
  metalens.undo.mockImplementation(token => server.undo(token))
  return server
}
