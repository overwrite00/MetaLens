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
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || res.statusText)
  }
  return res.json()
}

export const metalens = {
  health:    ()                    => api('GET',  '/health'),
  list:      (path)                => api('GET',  `/list?path=${encodeURIComponent(path)}`),
  read:      (path)                => api('GET',  `/read?path=${encodeURIComponent(path)}`),
  write:     (path, fields)        => api('POST', '/write',  { path, fields }),
  delete:    (path, keys)          => api('POST', '/delete', { path, keys }),
  diff:      (a, b)                => api('GET',  `/diff?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`),
  hash:      (path, algorithms)    => api('GET',  `/hash?path=${encodeURIComponent(path)}&algorithms=${encodeURIComponent(algorithms)}`),
}
